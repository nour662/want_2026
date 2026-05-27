import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Generator
import requests
import psycopg2
from psycopg2.extensions import connection


BASE_URL = "https://api.sam.gov/data-services/v1/extracts"


def safe_filename(name: str) -> str:
    name = name.strip().strip('"').strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def get_filename_from_headers(response: requests.Response, fallback: str) -> str:
    cd = response.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", cd, flags=re.IGNORECASE)
    if match:
        return safe_filename(match.group(1))
    return safe_filename(fallback)


def download_sam_extract(
    api_key: str,
    output_dir: Path,
    params: Dict[str, str],
    fallback_filename: str,
    timeout: int = 300
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "X-Api-Key": api_key,
        "Accept": "application/zip",
    }

    request_params = dict(params)
    response = requests.get(
        BASE_URL,
        headers=headers,
        params=request_params,
        stream=True,
        timeout=timeout,
    )

    if response.status_code in {401, 403}:
        response.close()
        request_params["api_key"] = api_key
        response = requests.get(
            BASE_URL,
            headers={"Accept": "application/zip"},
            params=request_params,
            stream=True,
            timeout=timeout,
        )

    with response:
        response.raise_for_status()

        filename = get_filename_from_headers(response, fallback_filename)
        output_path = output_dir / filename

        with open(output_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)

    return output_path


def download_entity_extract(
    api_key: str,
    output_dir: Path,
    frequency: str = "MONTHLY",
    charset: str = "UTF8",
    version: str = "V2"
) -> Path:
    params = {
        "fileType": "ENTITY",
        "sensitivity": "PUBLIC",
        "frequency": frequency,
        "charset": charset,
        "version": version,
    }

    return download_sam_extract(
        api_key,
        output_dir,
        params,
        f"SAM_PUBLIC_ENTITY_{frequency}.zip"
    )


def get_api_key(env_var: str = "SAM_API_KEY") -> str:
    api_key = os.getenv(env_var)
    if not api_key:
        raise ValueError(f"Environment variable {env_var} not set. Please set your SAM.gov API key.")
    return api_key


def detect_field_count(dat_file_path: Path) -> int:
    with dat_file_path.open("r", errors="ignore") as file:
        for line in file:
            line = line.rstrip("\n")

            if not line or line.startswith("BOF "):
                continue

            if line.endswith("!end"):
                line = line[:-4]

            fields = line.split("|")
            return len(fields)

    raise ValueError(f"No data records found in {dat_file_path}")


def parse_sam_dat_line(line: str, expected_fields: Optional[int] = None) -> Optional[List[str]]:
    line = line.rstrip("\n")

    if not line or line.startswith("BOF ") or line.startswith("EOF "):
        return None

    if line.endswith("!end"):
        line = line[:-4]

    fields = line.split("|")

    if expected_fields is not None and len(fields) != expected_fields:
        return None

    return fields


def parse_sam_dat_file(
    dat_file_path: Path,
    expected_fields: Optional[int] = None
) -> Generator[List[str], None, Tuple[int, int]]:
    if expected_fields is None:
        expected_fields = detect_field_count(dat_file_path)

    valid_count = 0
    skipped_count = 0

    with dat_file_path.open("r", errors="ignore") as file:
        for line in file:
            fields = parse_sam_dat_line(line, expected_fields)

            if fields is None:
                skipped_count += 1
                continue

            valid_count += 1
            yield fields

    return valid_count, skipped_count


def get_database_connection(
    dsn: Optional[str] = None,
    **kwargs
) -> connection:
    if dsn is None:
        dsn = os.getenv("PG_DSN", "dbname=sam user=postgres host=localhost port=5432")

    return psycopg2.connect(dsn, **kwargs)


def create_sam_table(
    conn: connection,
    schema: str,
    table: str,
    num_fields: int,
    drop_if_exists: bool = True
) -> None:
    field_columns = ",\n  ".join([f"f{i:03d} text" for i in range(1, num_fields + 1)])

    drop_clause = f"DROP TABLE IF EXISTS {schema}.{table};" if drop_if_exists else ""

    ddl = f"""
    CREATE SCHEMA IF NOT EXISTS {schema};

    {drop_clause}

    CREATE TABLE {schema}.{table} (
      load_id text NOT NULL,
      row_no  bigserial,
      {field_columns}
    );
    """

    with conn.cursor() as cursor:
        cursor.execute(ddl)
    conn.commit()


def load_sam_data_to_db(
    conn: connection,
    schema: str,
    table: str,
    dat_file_path: Path,
    load_id: Optional[str] = None,
    num_fields: Optional[int] = None
) -> Tuple[int, int]:
    if num_fields is None:
        num_fields = detect_field_count(dat_file_path)

    if load_id is None:
        load_id = dat_file_path.stem

    col_list = ", ".join(["load_id"] + [f"f{i:03d}" for i in range(1, num_fields + 1)])
    copy_sql = f"COPY {schema}.{table} ({col_list}) FROM STDIN WITH (FORMAT CSV, DELIMITER '|', QUOTE E'\\b', ESCAPE E'\\b');"

    inserted = 0
    skipped = 0

    def row_generator():
        nonlocal inserted, skipped

        with dat_file_path.open("r", errors="ignore") as file:
            for line in file:
                fields = parse_sam_dat_line(line, num_fields)

                if fields is None:
                    skipped += 1
                    continue

                yield load_id + "|" + "|".join(fields) + "\n"
                inserted += 1

    with conn.cursor() as cursor:
        cursor.copy_expert(copy_sql, row_generator())
    conn.commit()

    return inserted, skipped


def setup_and_load_sam_data(
    dat_file_path: Path,
    load_id: Optional[str] = None,
    schema: Optional[str] = None,
    table: Optional[str] = None,
    db_dsn: Optional[str] = None
) -> Dict[str, any]:
    if schema is None:
        schema = os.getenv("PG_SCHEMA", "sam_raw")

    if table is None:
        table = os.getenv("PG_TABLE", "entity_public_v2")

    num_fields = detect_field_count(dat_file_path)

    conn = get_database_connection(db_dsn)

    try:
        create_sam_table(conn, schema, table, num_fields)

        inserted, skipped = load_sam_data_to_db(
            conn, schema, table, dat_file_path, load_id, num_fields
        )

        return {
            "success": True,
            "schema": schema,
            "table": table,
            "num_fields": num_fields,
            "rows_inserted": inserted,
            "rows_skipped": skipped,
            "load_id": load_id or dat_file_path.stem
        }

    finally:
        conn.close()

