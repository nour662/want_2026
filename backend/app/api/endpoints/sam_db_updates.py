from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Depends
from pathlib import Path
from typing import Optional, List, Dict
from pydantic import BaseModel
from sqlalchemy.orm import Session
import tempfile
import os
import zipfile
from datetime import datetime

from app.api.endpoints.helper_funcs.sam_data_pipeline import (
    download_entity_extract,
    get_api_key,
    setup_and_load_sam_data,
    detect_field_count,
    parse_sam_dat_file
)
from app.models.sam_entity_public_v2 import SamEntityPublicV2
from app.core.db.session import get_db


router = APIRouter(
    prefix="/sam",
    tags=["sam_data"],
)


class DownloadRequest(BaseModel):
    extract_type: str
    frequency: Optional[str] = "MONTHLY"
    output_directory: Optional[str] = None


class DownloadResponse(BaseModel):
    success: bool
    message: str
    file_path: str


class LoadDataRequest(BaseModel):
    dat_file_path: str
    load_id: Optional[str] = None
    schema: Optional[str] = None
    table: Optional[str] = None


class LoadDataResponse(BaseModel):
    success: bool
    schema: str
    table: str
    num_fields: int
    rows_inserted: int
    rows_skipped: int
    load_id: str


class ParseDataResponse(BaseModel):
    success: bool
    num_fields: int
    total_records: int
    valid_records: int
    skipped_records: int


class FullSyncRequest(BaseModel):
    entity: bool = True
    truncate: bool = False
    entity_frequency: Optional[str] = "MONTHLY"
    batch_size: int = 5000


@router.post("/download/entity", response_model=DownloadResponse)
async def download_entity(request: DownloadRequest, background_tasks: BackgroundTasks):
    try:
        api_key = get_api_key()
        output_dir = Path(request.output_directory) if request.output_directory else Path("./sam_extracts")

        file_path = download_entity_extract(
            api_key=api_key,
            output_dir=output_dir,
            frequency=request.frequency
        )

        return DownloadResponse(
            success=True,
            message=f"Successfully downloaded entity extract",
            file_path=str(file_path)
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")


@router.post("/load", response_model=LoadDataResponse)
async def load_sam_data(request: LoadDataRequest):
    try:
        dat_file_path = Path(request.dat_file_path)

        if not dat_file_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {request.dat_file_path}")

        result = setup_and_load_sam_data(
            dat_file_path=dat_file_path,
            load_id=request.load_id,
            schema=request.schema,
            table=request.table
        )

        return LoadDataResponse(**result)

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Data file not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Load failed: {str(e)}")


@router.post("/parse", response_model=ParseDataResponse)
async def parse_sam_file(dat_file_path: str):
    try:
        file_path = Path(dat_file_path)

        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {dat_file_path}")

        num_fields = detect_field_count(file_path)

        valid_count = 0
        skipped_count = 0

        for _ in parse_sam_dat_file(file_path):
            valid_count += 1

        return ParseDataResponse(
            success=True,
            num_fields=num_fields,
            total_records=valid_count + skipped_count,
            valid_records=valid_count,
            skipped_records=skipped_count
        )

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Data file not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parse failed: {str(e)}")


@router.post("/upload-and-load")
async def upload_and_load(
    file: UploadFile = File(...),
    load_id: Optional[str] = None,
    schema: Optional[str] = None,
    table: Optional[str] = None
):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dat") as temp_file:
            content = await file.read()
            temp_file.write(content)
            temp_file_path = temp_file.name

        try:
            result = setup_and_load_sam_data(
                dat_file_path=Path(temp_file_path),
                load_id=load_id or file.filename,
                schema=schema,
                table=table
            )

            return LoadDataResponse(**result)

        finally:
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload and load failed: {str(e)}")


@router.post("/download-and-load")
async def download_and_load(
    extract_type: str,
    frequency: str = "MONTHLY",
    load_id: Optional[str] = None,
    schema: Optional[str] = None,
    table: Optional[str] = None
):
    try:
        api_key = get_api_key()
        output_dir = Path("./sam_extracts")

        if extract_type.lower() == "entity":
            file_path = download_entity_extract(api_key, output_dir, frequency)
            table = table or "sam_entity_public_v2"
        else:
            raise HTTPException(status_code=400, detail="Invalid extract_type. Use 'entity'")

        result = setup_and_load_sam_data(
            dat_file_path=file_path,
            load_id=load_id,
            schema=schema,
            table=table
        )

        return {
            "download": {
                "success": True,
                "file_path": str(file_path)
            },
            "load": result
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Operation failed: {str(e)}")


def extract_file_from_zip(zip_path: Path, suffix: str) -> Path:
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        candidates = [name for name in zip_ref.namelist() if name.lower().endswith(suffix)]
        if not candidates:
            raise HTTPException(status_code=500, detail=f"No {suffix} file found in {zip_path.name}")
        target = candidates[0]
        zip_ref.extract(target, zip_path.parent)
        return zip_path.parent / target


def parse_sam_entity_fields(fields: List[str]) -> Optional[Dict[str, str]]:
    if not fields:
        return None
    return {
        "uei": fields[0] if len(fields) > 0 else None,
        "cage_code": fields[3] if len(fields) > 3 else None,
        "registration_status": fields[5] if len(fields) > 5 else None,
        "entity_type_code": fields[6] if len(fields) > 6 else None,
        "registration_date": fields[7] if len(fields) > 7 else None,
        "expiration_date": fields[8] if len(fields) > 8 else None,
        "last_update_date_1": fields[9] if len(fields) > 9 else None,
        "last_update_date_2": fields[10] if len(fields) > 10 else None,
        "legal_business_name": fields[11] if len(fields) > 11 else None,
        "dba_name": fields[12] if len(fields) > 12 else None,
        "address_line1": fields[15] if len(fields) > 15 else None,
        "address_line2": fields[16] if len(fields) > 16 else None,
        "city": fields[17] if len(fields) > 17 else None,
        "state": fields[18] if len(fields) > 18 else None,
        "zip_code": fields[19] if len(fields) > 19 else None,
        "zip4": fields[20] if len(fields) > 20 else None,
        "country": fields[21] if len(fields) > 21 else None,
        "congressional_district": fields[22] if len(fields) > 22 else None,
    }


def insert_entities(db: Session, rows: List[Dict[str, object]]) -> int:
    db.bulk_insert_mappings(SamEntityPublicV2, rows)
    db.commit()
    return len(rows)


def upsert_entities(db: Session, rows: List[Dict[str, object]]) -> int:
    ueis = [row["uei"] for row in rows if row.get("uei")]
    existing = (
        db.query(SamEntityPublicV2.id, SamEntityPublicV2.uei)
        .filter(SamEntityPublicV2.uei.in_(ueis))
        .all()
    )
    existing_map = {uei: record_id for record_id, uei in existing}
    updates: List[Dict[str, object]] = []
    inserts: List[Dict[str, object]] = []

    for row in rows:
        uei = row.get("uei")
        if uei in existing_map:
            updates.append({"id": existing_map[uei], **row})
        else:
            inserts.append(row)

    if updates:
        db.bulk_update_mappings(SamEntityPublicV2, updates)
    if inserts:
        db.bulk_insert_mappings(SamEntityPublicV2, inserts)
    db.commit()
    return len(rows)


@router.post("/full-sync")
async def full_sync(request: FullSyncRequest, db: Session = Depends(get_db)):
    api_key = get_api_key()
    temp_dir = Path(tempfile.mkdtemp())
    entity_count = 0

    try:
        if request.entity:
            entity_zip = download_entity_extract(
                api_key=api_key,
                output_dir=temp_dir,
                frequency=request.entity_frequency or "MONTHLY",
            )
            entity_dat = extract_file_from_zip(entity_zip, ".dat")
            num_fields = detect_field_count(entity_dat)
            load_id = f"ENTITY_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            if request.truncate:
                db.query(SamEntityPublicV2).delete()
                db.commit()
            batch: List[Dict[str, object]] = []
            for row_no, fields in enumerate(parse_sam_dat_file(entity_dat, num_fields), start=1):
                entity_data = parse_sam_entity_fields(fields)
                if not entity_data or not entity_data.get("uei"):
                    continue
                raw_line = "|".join(fields)
                batch.append(
                    {
                        "load_id": load_id,
                        "row_no": row_no,
                        **entity_data,
                        "raw_line": raw_line,
                        "raw_fields": fields,
                    }
                )
                if len(batch) >= request.batch_size:
                    entity_count += insert_entities(db, batch) if request.truncate else upsert_entities(db, batch)
                    batch = []
            if batch:
                entity_count += insert_entities(db, batch) if request.truncate else upsert_entities(db, batch)

        return {
            "success": True,
            "entity_rows": entity_count,
        }
    finally:
        for item in temp_dir.glob("*"):
            try:
                item.unlink()
            except OSError:
                pass
        try:
            temp_dir.rmdir()
        except OSError:
            pass

