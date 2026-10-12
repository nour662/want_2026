"""
Bulk WATN data import endpoint.
Supports Excel (.xlsx, .xls) and CSV files for:
- Cohorts
- Companies
- Participants
- Funding
"""

import logging
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.models.events import Event
from app.models.participant_lists import ParticipantList
from app.services.data_ingestion import (
    get_default_hub,
    get_system_user,
    ingest_all_watn_files,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/participants", tags=["participants"])


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class BulkUploadResponse(BaseModel):
    cohorts_created: int = 0
    cohorts_found: int = 0
    cohorts_merged: int = 0
    companies_created: int = 0
    companies_merged: int = 0
    participants_inserted: int = 0
    participants_errors: int = 0
    funding_created: int = 0
    funding_skipped: int = 0
    participant_list_ids: Dict[str, str] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)


def _validate_file_extension(upload_file: Optional[UploadFile], name: str):
    if not upload_file:
        return
    fname = upload_file.filename or ""
    lower = fname.lower()
    if not (lower.endswith(".xlsx") or lower.endswith(".xls") or lower.endswith(".csv")):
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be a .xlsx, .xls, or .csv file (received '{fname}')",
        )


@router.post(
    "/upload/bulk",
    response_model=BulkUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def bulk_upload_files(
    hub_id: Optional[UUID] = Form(default=None),
    published_by_user_id: Optional[UUID] = Form(default=None),
    participants_file: Optional[UploadFile] = File(default=None),
    cohorts_file: Optional[UploadFile] = File(default=None),
    companies_file: Optional[UploadFile] = File(default=None),
    funding_file: Optional[UploadFile] = File(default=None),
    db: Session = Depends(get_db),
):
    """
    Import cohorts, companies, participants, and/or funding from Excel (.xlsx/.xls) or CSV files.
    Idempotent: only inserts new records and updates empty fields on existing records.
    """
    if not any([participants_file, cohorts_file, companies_file, funding_file]):
        raise HTTPException(
            status_code=400,
            detail="At least one file must be provided (participants_file, cohorts_file, companies_file, or funding_file).",
        )

    _validate_file_extension(cohorts_file, "cohorts_file")
    _validate_file_extension(companies_file, "companies_file")
    _validate_file_extension(participants_file, "participants_file")
    _validate_file_extension(funding_file, "funding_file")

    target_hub_id = hub_id or get_default_hub(db).id

    # Read bytes for provided files
    cohorts_bytes = cohorts_file.file.read() if cohorts_file else None
    companies_bytes = companies_file.file.read() if companies_file else None
    participants_bytes = participants_file.file.read() if participants_file else None
    funding_bytes = funding_file.file.read() if funding_file else None

    result = ingest_all_watn_files(
        db=db,
        cohorts_source=cohorts_bytes,
        companies_source=companies_bytes,
        participants_source=participants_bytes,
        funding_source=funding_bytes,
        hub_id=target_hub_id,
    )

    cohorts_stat = result.get("cohorts", {})
    companies_stat = result.get("companies", {})
    participants_stat = result.get("participants", {})
    funding_stat = result.get("funding", {})

    all_errors = list(result.get("errors", []))
    for stage in ("cohorts", "companies", "participants", "funding"):
        if stage in result and result[stage].get("errors"):
            all_errors.extend(result[stage]["errors"])

    # Build participant list mapping
    plist_map: Dict[str, str] = {}
    plists = (
        db.query(ParticipantList, Event.name)
        .join(Event, Event.id == ParticipantList.event_id)
        .filter(Event.hub_organization_id == target_hub_id)
        .all()
    )
    for plist, event_name in plists:
        if event_name:
            plist_map[event_name] = str(plist.id)

    return BulkUploadResponse(
        cohorts_created=cohorts_stat.get("events_created", 0),
        cohorts_found=cohorts_stat.get("total_records", 0) - cohorts_stat.get("events_created", 0),
        cohorts_merged=cohorts_stat.get("cohorts_updated", 0),
        companies_created=companies_stat.get("companies_created", 0),
        companies_merged=companies_stat.get("companies_updated", 0),
        participants_inserted=participants_stat.get("entries_created", 0),
        participants_errors=len(participants_stat.get("errors", [])),
        funding_created=funding_stat.get("funding_created", 0),
        funding_skipped=funding_stat.get("funding_skipped_existing", 0),
        participant_list_ids=plist_map,
        errors=all_errors[:20],
    )
