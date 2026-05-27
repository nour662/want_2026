import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.orm import Session

from app.api.endpoints.helper_funcs.sam_data_pipeline import (
    detect_field_count,
    download_entity_extract,
    get_api_key,
    parse_sam_dat_file,
)
from app.core.db.base import SessionLocal
from app.core.db.session import get_db
from app.models.company import Company
from app.models.sam_entity_public_v2 import SamEntityPublicV2

router = APIRouter(
    prefix="/sam/sync",
    tags=["sam_sync"],
)

SYNC_FREQUENCY = os.getenv("SAM_SYNC_FREQUENCY", "DAILY").upper()
SYNC_BATCH_SIZE = max(100, int(os.getenv("SAM_SYNC_BATCH_SIZE", "5000")))

FIELD_LENGTH_LIMITS = {
    "uei": 12,
    "cage_code": 5,
    "registration_status": 16,
    "entity_type_code": 16,
    "registration_date": 8,
    "expiration_date": 8,
    "last_update_date_1": 8,
    "last_update_date_2": 8,
    "state": 64,
    "zip_code": 16,
    "zip4": 16,
    "country": 8,
    "congressional_district": 16,
}


class SyncResponse(BaseModel):
    success: bool
    message: str
    records_processed: int
    new_records: int
    updated_records: int
    skipped_records: int = 0
    errors: int
    timestamp: str


class SyncStatusResponse(BaseModel):
    is_running: bool
    last_sync: Optional[str] = None
    last_sync_status: Optional[str] = None
    records_processed: Optional[int] = None
    new_records: Optional[int] = None
    updated_records: Optional[int] = None
    skipped_records: Optional[int] = None
    errors: Optional[int] = None
    last_error: Optional[str] = None
    database_records: int = 0
    company_records: int = 0


class SamPreviewRow(BaseModel):
    id: int
    load_id: Optional[str] = None
    uei: Optional[str] = None
    legal_business_name: Optional[str] = None
    dba_name: Optional[str] = None
    registration_status: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None


class SamPreviewResponse(BaseModel):
    table_name: str
    showing: int
    rows: List[SamPreviewRow]


sync_status = {
    "is_running": False,
    "last_sync": None,
    "last_sync_status": None,
    "records_processed": 0,
    "new_records": 0,
    "updated_records": 0,
    "skipped_records": 0,
    "errors": 0,
    "last_error": None,
}


def extract_dat_file_from_zip(zip_path: Path, extract_to: Path) -> Optional[Path]:
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        dat_files = [item for item in zip_ref.namelist() if item.lower().endswith(".dat")]
        if not dat_files:
            raise ValueError("No .dat file found in zip archive")

        dat_file = dat_files[0]
        zip_ref.extract(dat_file, extract_to)
        return extract_to / dat_file


def _clean_value(value: Optional[str], max_length: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if max_length is not None:
        cleaned = cleaned[:max_length]
    return cleaned or None


def ensure_sam_entity_schema(db: Session) -> None:
    statements = [
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN registration_status TYPE VARCHAR(16)",
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN entity_type_code TYPE VARCHAR(16)",
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN state TYPE VARCHAR(64)",
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN zip_code TYPE VARCHAR(16)",
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN zip4 TYPE VARCHAR(16)",
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN country TYPE VARCHAR(8)",
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN congressional_district TYPE VARCHAR(16)",
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN incorporation_state TYPE VARCHAR(64)",
        "ALTER TABLE IF EXISTS sam_entity_public_v2 ALTER COLUMN incorporation_country TYPE VARCHAR(8)",
    ]
    for statement in statements:
        db.execute(text(statement))
    db.commit()


def parse_sam_entity_fields(fields: List[str]) -> Optional[Dict[str, Optional[str]]]:
    if not fields:
        return None

    return {
        "uei": _clean_value(fields[0], FIELD_LENGTH_LIMITS["uei"]) if len(fields) > 0 else None,
        "cage_code": _clean_value(fields[3], FIELD_LENGTH_LIMITS["cage_code"]) if len(fields) > 3 else None,
        "registration_status": _clean_value(fields[5], FIELD_LENGTH_LIMITS["registration_status"]) if len(fields) > 5 else None,
        "entity_type_code": _clean_value(fields[6], FIELD_LENGTH_LIMITS["entity_type_code"]) if len(fields) > 6 else None,
        "registration_date": _clean_value(fields[7], FIELD_LENGTH_LIMITS["registration_date"]) if len(fields) > 7 else None,
        "expiration_date": _clean_value(fields[8], FIELD_LENGTH_LIMITS["expiration_date"]) if len(fields) > 8 else None,
        "last_update_date_1": _clean_value(fields[9], FIELD_LENGTH_LIMITS["last_update_date_1"]) if len(fields) > 9 else None,
        "last_update_date_2": _clean_value(fields[10], FIELD_LENGTH_LIMITS["last_update_date_2"]) if len(fields) > 10 else None,
        "legal_business_name": _clean_value(fields[11]) if len(fields) > 11 else None,
        "dba_name": _clean_value(fields[12]) if len(fields) > 12 else None,
        "address_line1": _clean_value(fields[15]) if len(fields) > 15 else None,
        "address_line2": _clean_value(fields[16]) if len(fields) > 16 else None,
        "city": _clean_value(fields[17]) if len(fields) > 17 else None,
        "state": _clean_value(fields[18], FIELD_LENGTH_LIMITS["state"]) if len(fields) > 18 else None,
        "zip_code": _clean_value(fields[19], FIELD_LENGTH_LIMITS["zip_code"]) if len(fields) > 19 else None,
        "zip4": _clean_value(fields[20], FIELD_LENGTH_LIMITS["zip4"]) if len(fields) > 20 else None,
        "country": _clean_value(fields[21], FIELD_LENGTH_LIMITS["country"]) if len(fields) > 21 else None,
        "congressional_district": _clean_value(fields[22], FIELD_LENGTH_LIMITS["congressional_district"]) if len(fields) > 22 else None,
    }


def _dedupe_by_uei(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    deduped: Dict[str, Dict[str, object]] = {}
    rows_without_uei: List[Dict[str, object]] = []

    for row in rows:
        uei = row.get("uei")
        if uei:
            deduped[str(uei)] = row
        else:
            rows_without_uei.append(row)

    return list(deduped.values()) + rows_without_uei


def upsert_sam_entities(db: Session, rows: List[Dict[str, object]]) -> Tuple[int, int]:
    if not rows:
        return 0, 0

    normalized_rows = _dedupe_by_uei(rows)
    ueis = sorted({str(row["uei"]) for row in normalized_rows if row.get("uei")})
    existing_rows = (
        db.query(SamEntityPublicV2.uei)
        .filter(SamEntityPublicV2.uei.in_(ueis))
        .all()
    )
    existing_ueis = {uei for (uei,) in existing_rows if uei}

    inserts = [
        row
        for row in normalized_rows
        if row.get("uei") not in existing_ueis
    ]
    skipped = len(normalized_rows) - len(inserts)

    if inserts:
        db.bulk_insert_mappings(SamEntityPublicV2, inserts)

    return len(inserts), skipped


def upsert_companies(db: Session, entities: List[Dict[str, Optional[str]]]) -> Tuple[int, int]:
    if not entities:
        return 0, 0

    normalized_entities = _dedupe_by_uei(entities)
    ueis = sorted({str(entity["uei"]) for entity in normalized_entities if entity.get("uei")})
    if not ueis:
        return 0, 0

    company_columns = {
        column["name"]
        for column in sa_inspect(db.bind).get_columns("companies")
    }
    name_column = "company_name" if "company_name" in company_columns else "name"
    if name_column not in company_columns:
        return 0, 0

    existing_rows = db.execute(
        text("SELECT id, uei FROM companies WHERE uei = ANY(:ueis)"),
        {"ueis": ueis},
    ).mappings().all()
    existing_map = {row["uei"]: row["id"] for row in existing_rows if row.get("uei")}

    optional_columns = [
        column_name
        for column_name in ["legal_name", "duns", "address1", "address2", "city", "state", "zip", "hq_country"]
        if column_name in company_columns
    ]

    insert_rows: List[Dict[str, object]] = []
    for entity in normalized_entities:
        uei = entity.get("uei")
        legal_name = entity.get("legal_business_name")
        if not uei or not legal_name or uei in existing_map:
            continue

        payload: Dict[str, object] = {
            name_column: legal_name,
            "uei": uei,
        }
        field_map = {
            "legal_name": legal_name,
            "duns": entity.get("cage_code"),
            "address1": entity.get("address_line1"),
            "address2": entity.get("address_line2"),
            "city": entity.get("city"),
            "state": entity.get("state"),
            "zip": entity.get("zip_code"),
            "hq_country": entity.get("country"),
        }
        for column_name in optional_columns:
            payload[column_name] = field_map.get(column_name)

        if "id" in company_columns:
            payload["id"] = str(uuid4())
        insert_rows.append(payload)

    if insert_rows:
        insert_columns = [name_column, "uei"] + optional_columns
        if "id" in company_columns:
            insert_columns = ["id"] + insert_columns
        placeholders = ", ".join(f":{column_name}" for column_name in insert_columns)
        db.execute(
            text(f"INSERT INTO companies ({', '.join(insert_columns)}) VALUES ({placeholders})"),
            insert_rows,
        )

    return len(insert_rows), 0


def process_sam_sync(db: Optional[Session] = None, max_rows: Optional[int] = None) -> Dict[str, object]:
    owns_session = db is None
    active_db = db or SessionLocal()

    sync_status.update(
        {
            "is_running": True,
            "last_sync_status": "running",
            "records_processed": 0,
            "new_records": 0,
            "updated_records": 0,
            "skipped_records": 0,
            "errors": 0,
            "last_error": None,
        }
    )

    records_processed = 0
    new_records = 0
    updated_records = 0
    skipped_records = 0
    errors = 0

    temp_dir: Optional[Path] = None
    zip_path: Optional[Path] = None
    dat_path: Optional[Path] = None

    try:
        ensure_sam_entity_schema(active_db)
        api_key = get_api_key()
        temp_dir = Path(tempfile.mkdtemp(prefix="sam_sync_"))

        try:
            zip_path = download_entity_extract(
                api_key=api_key,
                output_dir=temp_dir,
                frequency=SYNC_FREQUENCY,
            )
        except Exception:
            if SYNC_FREQUENCY == "MONTHLY":
                raise
            zip_path = download_entity_extract(
                api_key=api_key,
                output_dir=temp_dir,
                frequency="MONTHLY",
            )

        dat_path = extract_dat_file_from_zip(zip_path, temp_dir)
        num_fields = detect_field_count(dat_path)
        load_id = f"SAMSYNC_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        sam_batch: List[Dict[str, object]] = []
        company_batch: List[Dict[str, Optional[str]]] = []

        for row_no, fields in enumerate(parse_sam_dat_file(dat_path, num_fields), start=1):
            entity_data = parse_sam_entity_fields(fields)
            if not entity_data or not entity_data.get("uei") or not entity_data.get("legal_business_name"):
                errors += 1
                continue

            sam_batch.append(
                {
                    "load_id": load_id,
                    "row_no": row_no,
                    **entity_data,
                    "raw_line": "|".join(fields),
                    "raw_fields": fields,
                }
            )
            company_batch.append(entity_data)
            records_processed += 1

            if len(sam_batch) >= SYNC_BATCH_SIZE:
                raw_inserted, raw_skipped = upsert_sam_entities(active_db, sam_batch)
                active_db.commit()
                try:
                    upsert_companies(active_db, company_batch)
                    active_db.commit()
                except Exception as company_exc:
                    errors += len(company_batch)
                    active_db.rollback()
                    sync_status["last_error"] = f"Company sync warning: {company_exc}"
                new_records += raw_inserted
                skipped_records += raw_skipped
                sync_status["records_processed"] = records_processed
                sync_status["new_records"] = new_records
                sync_status["updated_records"] = updated_records
                sync_status["skipped_records"] = skipped_records
                sam_batch = []
                company_batch = []

            if max_rows is not None and records_processed >= max_rows:
                break

        if sam_batch:
            raw_inserted, raw_skipped = upsert_sam_entities(active_db, sam_batch)
            active_db.commit()
            try:
                upsert_companies(active_db, company_batch)
                active_db.commit()
            except Exception as company_exc:
                errors += len(company_batch)
                active_db.rollback()
                sync_status["last_error"] = f"Company sync warning: {company_exc}"
            new_records += raw_inserted
            skipped_records += raw_skipped

        sync_status.update(
            {
                "last_sync": datetime.utcnow().isoformat(),
                "last_sync_status": "success",
                "records_processed": records_processed,
                "new_records": new_records,
                "updated_records": updated_records,
                "skipped_records": skipped_records,
                "errors": errors,
                "last_error": None,
            }
        )

        return {
            "success": True,
            "message": "Successfully downloaded the SAM.gov extract and synced only brand-new UEIs into the database tables.",
            "records_processed": records_processed,
            "new_records": new_records,
            "updated_records": updated_records,
            "skipped_records": skipped_records,
            "errors": errors,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as exc:
        active_db.rollback()
        sync_status.update(
            {
                "last_sync": datetime.utcnow().isoformat(),
                "last_sync_status": "failed",
                "records_processed": records_processed,
                "new_records": new_records,
                "updated_records": updated_records,
                "skipped_records": skipped_records,
                "errors": errors,
                "last_error": str(exc),
            }
        )
        raise HTTPException(status_code=500, detail=f"Sync failed: {exc}") from exc
    finally:
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        sync_status["is_running"] = False
        if owns_session:
            active_db.close()


@router.post("/daily", response_model=SyncResponse)
async def run_daily_sync(
    background_tasks: BackgroundTasks,
    max_rows: Optional[int] = None,
    db: Session = Depends(get_db),
):
    if sync_status["is_running"]:
        raise HTTPException(status_code=409, detail="Sync is already running")

    if max_rows is not None and max_rows < 1:
        raise HTTPException(status_code=400, detail="max_rows must be greater than 0.")

    result = process_sam_sync(db, max_rows=max_rows)
    return SyncResponse(**result)


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status(db: Session = Depends(get_db)):
    database_records = db.query(SamEntityPublicV2).count()
    company_records = db.execute(
        text("SELECT COUNT(*) FROM companies WHERE uei IS NOT NULL")
    ).scalar() or 0
    return SyncStatusResponse(
        **sync_status,
        database_records=database_records,
        company_records=int(company_records),
    )


@router.get("/preview", response_model=SamPreviewResponse)
async def get_sam_table_preview(limit: int = 12, db: Session = Depends(get_db)):
    preview_limit = max(1, min(limit, 50))
    rows = (
        db.query(SamEntityPublicV2)
        .order_by(SamEntityPublicV2.id.desc())
        .limit(preview_limit)
        .all()
    )

    return SamPreviewResponse(
        table_name="public.sam_entity_public_v2",
        showing=len(rows),
        rows=[
            SamPreviewRow(
                id=row.id,
                load_id=row.load_id,
                uei=row.uei,
                legal_business_name=row.legal_business_name,
                dba_name=row.dba_name,
                registration_status=row.registration_status,
                city=row.city,
                state=row.state,
                country=row.country,
            )
            for row in rows
        ],
    )


@router.post("/trigger-background")
async def trigger_background_sync(background_tasks: BackgroundTasks):
    if sync_status["is_running"]:
        raise HTTPException(status_code=409, detail="Sync is already running")

    background_tasks.add_task(process_sam_sync)

    return {
        "success": True,
        "message": "SAM.gov sync started in the background.",
    }
