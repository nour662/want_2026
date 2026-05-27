import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import inspect as sa_inspect, or_, text
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.models.events import Event
from app.models.match_jobs import MatchJob
from app.models.participant_lists import ParticipantList
from app.models.participants import ParticipantListEntry, Participants

router = APIRouter(prefix="/participants", tags=["participants"])

REQUIRED_COLUMNS = {
    "team_name",
    "first_name",
    "last_name",
    "email",
    "team_description",
}

OPTIONAL_COLUMN_ALIASES = {
    "university_affiliation": ["university_affiliation", "affiliation", "university"],
    "linkedin_url": ["linkedin_url", "linkedin"],
    "optional_uei": ["optional_uei", "uei"],
    "optional_duns": ["optional_duns", "duns"],
    "company_website": ["company_website", "website", "company_url", "url"],
    "role": ["role", "title", "participant_title"],
}


class ParticipantEntryResponse(BaseModel):
    id: UUID
    participant_list_id: UUID
    participant_id: Optional[UUID] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    attached_company_name: Optional[str] = None
    raw_full_name: Optional[str] = None
    raw_email: Optional[str] = None
    raw_affiliation: Optional[str] = None
    raw_team_name: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ParticipantListResponse(BaseModel):
    participants: List[ParticipantEntryResponse]
    total: int


class UploadResponse(BaseModel):
    inserted: int
    errors: int
    participant_list_id: UUID
    match_job_id: Optional[UUID] = None


class ParticipantDirectoryItemResponse(BaseModel):
    id: UUID
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    primary_affiliation: Optional[str] = None
    latest_team_name: Optional[str] = None
    attached_company_name: Optional[str] = None
    team_record_count: int = 0
    cohort_count: int = 0
    last_uploaded_at: datetime

    class Config:
        from_attributes = True


class ParticipantDirectoryResponse(BaseModel):
    participants: List[ParticipantDirectoryItemResponse]
    total: int


class ParticipantAssociatedCohortResponse(BaseModel):
    participant_list_entry_id: UUID
    cohort_id: Optional[UUID] = None
    cohort_name: Optional[str] = None
    cohort_type: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    team_name: Optional[str] = None
    affiliation: Optional[str] = None
    title: Optional[str] = None
    notes: Optional[str] = None
    attached_company_name: Optional[str] = None
    uploaded_at: datetime


class ParticipantProfileResponse(BaseModel):
    id: UUID
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    affiliations: List[str] = Field(default_factory=list)
    team_names: List[str] = Field(default_factory=list)
    attached_company_names: List[str] = Field(default_factory=list)
    team_record_count: int = 0
    cohort_count: int = 0
    associated_events: List[ParticipantAssociatedCohortResponse] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


def _get_company_name_column(db: Session) -> Optional[str]:
    try:
        company_columns = {
            column["name"]
            for column in sa_inspect(db.bind).get_columns("companies")
        }
    except Exception:
        return None

    if "name" in company_columns:
        return "name"
    if "company_name" in company_columns:
        return "company_name"
    return None


def _get_attached_company_name(
    entry_id: UUID,
    db: Session,
    company_name_column: Optional[str],
) -> Optional[str]:
    if not company_name_column:
        return None

    statement = text(
        f"""
        SELECT companies.{company_name_column}
        FROM companies
        JOIN participant_company_matches
          ON participant_company_matches.company_id = companies.id
        WHERE participant_company_matches.participant_list_entry_id = :entry_id
          AND participant_company_matches.is_active IS TRUE
        ORDER BY participant_company_matches.confidence DESC,
                 participant_company_matches.created_at DESC
        LIMIT 1
        """
    )
    return db.execute(statement, {"entry_id": str(entry_id)}).scalar()


def _serialize_participant_entry(
    entry: ParticipantListEntry,
    db: Session,
    company_name_column: Optional[str],
) -> ParticipantEntryResponse:
    participant = None
    if entry.participant_id:
        participant = db.query(Participants).filter(Participants.id == entry.participant_id).first()

    attached_company_name = _get_attached_company_name(entry.id, db, company_name_column)

    return ParticipantEntryResponse(
        id=entry.id,
        participant_list_id=entry.participant_list_id,
        participant_id=entry.participant_id,
        first_name=participant.first_name if participant else None,
        last_name=participant.last_name if participant else None,
        email=(participant.primary_email if participant else entry.raw_email),
        linkedin_url=participant.linkedin_url if participant else None,
        attached_company_name=attached_company_name,
        raw_full_name=entry.raw_full_name,
        raw_email=entry.raw_email,
        raw_affiliation=entry.raw_affiliation,
        raw_team_name=entry.raw_team_name,
        notes=entry.notes,
        created_at=entry.created_at,
    )


def _merge_participant_record(
    participant: Participants,
    first_name: str,
    last_name: str,
    full_name: str,
    linkedin_url: Optional[str],
) -> None:
    if first_name and participant.first_name in {None, "", "Unknown"}:
        participant.first_name = first_name
    if last_name and participant.last_name in {None, "", "Unknown"}:
        participant.last_name = last_name
    if full_name and not getattr(participant, "_full_name", None):
        participant.full_name = full_name
    if linkedin_url and not participant.linkedin_url:
        participant.linkedin_url = linkedin_url


def _get_or_merge_participant_by_email(
    email: str,
    first_name: str,
    last_name: str,
    full_name: str,
    linkedin_url: Optional[str],
    participant_cache: Dict[str, Participants],
    db: Session,
) -> Participants:
    participant = participant_cache.get(email)
    if participant is None:
        participant = (
            db.query(Participants)
            .filter(Participants.primary_email == email)
            .first()
        )

    if not participant:
        participant = Participants(
            primary_email=email,
            first_name=first_name or "Unknown",
            last_name=last_name or "Unknown",
            full_name=full_name or None,
            linkedin_url=linkedin_url,
        )
        db.add(participant)
        db.flush()

    _merge_participant_record(participant, first_name, last_name, full_name, linkedin_url)
    db.flush()
    participant_cache[email] = participant
    return participant


def _get_participant_entry_rows(
    participant_ids: List[UUID],
    db: Session,
) -> Dict[UUID, List[tuple[ParticipantListEntry, Event]]]:
    grouped_rows: Dict[UUID, List[tuple[ParticipantListEntry, Event]]] = defaultdict(list)
    if not participant_ids:
        return grouped_rows

    rows = (
        db.query(ParticipantListEntry, Event)
        .join(ParticipantList, ParticipantListEntry.participant_list_id == ParticipantList.id)
        .join(Event, ParticipantList.event_id == Event.id)
        .filter(ParticipantListEntry.participant_id.in_(participant_ids))
        .order_by(Event.start_date.desc().nullslast(), ParticipantListEntry.created_at.desc())
        .all()
    )

    for entry, event in rows:
        grouped_rows[entry.participant_id].append((entry, event))

    return grouped_rows


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
def upload_participants_csv(
    event_id: UUID = Form(...),
    published_by_user_id: UUID = Form(...),
    university_id: Optional[UUID] = Form(None),
    mapping_json: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSV file required")

    raw_content = file.file.read()
    try:
        text = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV missing header row")

    fieldnames = [name.strip() for name in reader.fieldnames]
    field_map: Dict[str, Optional[str]] = {key: key for key in REQUIRED_COLUMNS}
    for optional_key in OPTIONAL_COLUMN_ALIASES:
        field_map[optional_key] = None

    if mapping_json:
        try:
            mapping = json.loads(mapping_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid mapping JSON") from exc

        if not isinstance(mapping, dict):
            raise HTTPException(status_code=400, detail="Mapping must be an object")

        for required_field in REQUIRED_COLUMNS:
            mapped = mapping.get(required_field)
            if not mapped:
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing mapping for {required_field}",
                )
            if mapped not in fieldnames:
                raise HTTPException(
                    status_code=400,
                    detail=f"Mapped column '{mapped}' not found in CSV",
                )
            field_map[required_field] = mapped

        for optional_key, aliases in OPTIONAL_COLUMN_ALIASES.items():
            mapped = next((mapping.get(alias) for alias in [optional_key, *aliases] if mapping.get(alias)), None)
            if mapped:
                if mapped not in fieldnames:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Mapped column '{mapped}' not found in CSV",
                    )
                field_map[optional_key] = mapped
    else:
        missing = REQUIRED_COLUMNS - set(fieldnames)
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required columns: {', '.join(sorted(missing))}",
            )

        normalized_headers = {
            name: name.lower().replace("_", " ").strip()
            for name in fieldnames
        }
        for optional_key, aliases in OPTIONAL_COLUMN_ALIASES.items():
            field_map[optional_key] = next(
                (
                    name
                    for name, normalized in normalized_headers.items()
                    if any(alias.replace("_", " ") in normalized for alias in aliases)
                ),
                None,
            )

    participant_list = ParticipantList(
        event_id=event_id,
        published_by_user_id=published_by_user_id,
        university_id=university_id,
        title="Participant Upload",
        source_type="csv",
        source_filename=file.filename,
        status="draft",
    )
    db.add(participant_list)
    db.flush()

    inserted = 0
    errors = 0
    participant_cache: Dict[str, Participants] = {}
    for row_number, row in enumerate(reader, start=1):
        email = (row.get(field_map["email"]) or "").strip().lower()
        if not email:
            errors += 1
            continue

        first_name = (row.get(field_map["first_name"]) or "").strip()
        last_name = (row.get(field_map["last_name"]) or "").strip()
        full_name = " ".join([part for part in [first_name, last_name] if part]).strip()

        linkedin_value = None
        linkedin_field = field_map.get("linkedin_url")
        if linkedin_field:
            linkedin_value = (row.get(linkedin_field) or "").strip() or None

        participant = _get_or_merge_participant_by_email(
            email=email,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            linkedin_url=linkedin_value,
            participant_cache=participant_cache,
            db=db,
        )

        notes_parts = []
        team_description = (row.get(field_map["team_description"]) or "").strip()
        if team_description:
            notes_parts.append(team_description)

        optional_uei_field = field_map.get("optional_uei")
        optional_duns_field = field_map.get("optional_duns")
        company_website_field = field_map.get("company_website")
        affiliation_field = field_map.get("university_affiliation")
        role_field = field_map.get("role")

        optional_uei = ((row.get(optional_uei_field) if optional_uei_field else None) or "").strip()
        optional_duns = ((row.get(optional_duns_field) if optional_duns_field else None) or "").strip()
        company_website = ((row.get(company_website_field) if company_website_field else None) or "").strip()
        raw_affiliation = ((row.get(affiliation_field) if affiliation_field else None) or "").strip() or None
        raw_title = ((row.get(role_field) if role_field else None) or row.get("role") or row.get("title") or "").strip() or None

        if optional_uei:
            notes_parts.append(f"UEI: {optional_uei}")
        if optional_duns:
            notes_parts.append(f"DUNS: {optional_duns}")
        if company_website:
            notes_parts.append(f"Website: {company_website}")
        notes = " | ".join(notes_parts) if notes_parts else None

        entry = ParticipantListEntry(
            participant_list_id=participant_list.id,
            participant_id=participant.id,
            row_number=row_number,
            raw_full_name=full_name or None,
            raw_email=email,
            raw_affiliation=raw_affiliation,
            raw_title=raw_title,
            raw_team_name=(row.get(field_map["team_name"]) or "").strip() or None,
            notes=notes,
        )
        db.add(entry)
        inserted += 1

    participant_list.status = "uploaded" if inserted else "draft"

    match_job = None

    db.commit()
    if match_job:
        db.refresh(match_job)
    db.refresh(participant_list)

    return UploadResponse(
        inserted=inserted,
        errors=errors,
        participant_list_id=participant_list.id,
        match_job_id=match_job.id if match_job else None,
    )


@router.get("/", response_model=ParticipantListResponse)
def list_participants(
    participant_list_id: Optional[UUID] = None,
    event_id: Optional[UUID] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    company_name_column = _get_company_name_column(db)
    query = db.query(ParticipantListEntry)
    if event_id:
        query = query.join(
            ParticipantList,
            ParticipantListEntry.participant_list_id == ParticipantList.id,
        ).filter(ParticipantList.event_id == event_id)
    if participant_list_id:
        query = query.filter(ParticipantListEntry.participant_list_id == participant_list_id)
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                ParticipantListEntry.raw_full_name.ilike(term),
                ParticipantListEntry.raw_email.ilike(term),
                ParticipantListEntry.raw_affiliation.ilike(term),
                ParticipantListEntry.raw_team_name.ilike(term),
            )
        )

    total = query.count()
    entries = (
        query.order_by(ParticipantListEntry.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return ParticipantListResponse(
        participants=[
            _serialize_participant_entry(entry, db, company_name_column)
            for entry in entries
        ],
        total=total,
    )


@router.get("/directory", response_model=ParticipantDirectoryResponse)
def list_participant_directory(
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = db.query(Participants).join(
        ParticipantListEntry,
        ParticipantListEntry.participant_id == Participants.id,
    )
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Participants.first_name.ilike(term),
                Participants.last_name.ilike(term),
                Participants.primary_email.ilike(term),
                Participants._full_name.ilike(term),
                ParticipantListEntry.raw_full_name.ilike(term),
                ParticipantListEntry.raw_affiliation.ilike(term),
                ParticipantListEntry.raw_team_name.ilike(term),
            )
        )

    total = query.with_entities(Participants.id).distinct().count()
    participants = (
        query.distinct()
        .order_by(Participants.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    company_name_column = _get_company_name_column(db)
    grouped_rows = _get_participant_entry_rows([participant.id for participant in participants], db)

    response_items = []
    for participant in participants:
        participant_rows = grouped_rows.get(participant.id, [])
        latest_entry = participant_rows[0][0] if participant_rows else None
        cohort_ids = {event.id for _, event in participant_rows}
        full_name = (
            (participant.full_name or "").strip()
            or (latest_entry.raw_full_name if latest_entry and latest_entry.raw_full_name else "")
            or participant.primary_email
        )

        response_items.append(
            ParticipantDirectoryItemResponse(
                id=participant.id,
                first_name=participant.first_name,
                last_name=participant.last_name,
                full_name=full_name,
                email=participant.primary_email,
                linkedin_url=participant.linkedin_url,
                primary_affiliation=next(
                    (entry.raw_affiliation for entry, _ in participant_rows if entry.raw_affiliation),
                    None,
                ),
                latest_team_name=next(
                    (entry.raw_team_name for entry, _ in participant_rows if entry.raw_team_name),
                    None,
                ),
                attached_company_name=(
                    _get_attached_company_name(latest_entry.id, db, company_name_column)
                    if latest_entry
                    else None
                ),
                team_record_count=len(participant_rows),
                cohort_count=len(cohort_ids),
                last_uploaded_at=latest_entry.created_at if latest_entry else participant.created_at,
            )
        )

    return ParticipantDirectoryResponse(participants=response_items, total=total)


@router.get("/directory/{participant_id}", response_model=ParticipantProfileResponse)
def get_participant_profile(
    participant_id: UUID,
    db: Session = Depends(get_db),
):
    participant = db.query(Participants).filter(Participants.id == participant_id).first()
    if not participant:
        raise HTTPException(status_code=404, detail="Participant not found")

    company_name_column = _get_company_name_column(db)
    participant_rows = _get_participant_entry_rows([participant_id], db).get(participant_id, [])

    associated_events = []
    affiliations = []
    team_names = []
    attached_company_names = []

    for entry, event in participant_rows:
        attached_company_name = _get_attached_company_name(entry.id, db, company_name_column)
        if entry.raw_affiliation and entry.raw_affiliation not in affiliations:
            affiliations.append(entry.raw_affiliation)
        if entry.raw_team_name and entry.raw_team_name not in team_names:
            team_names.append(entry.raw_team_name)
        if attached_company_name and attached_company_name not in attached_company_names:
            attached_company_names.append(attached_company_name)

        associated_events.append(
            ParticipantAssociatedCohortResponse(
                participant_list_entry_id=entry.id,
                cohort_id=event.id,
                cohort_name=event.name,
                cohort_type=event.event_type,
                start_date=event.start_date,
                end_date=event.end_date,
                team_name=entry.raw_team_name,
                affiliation=entry.raw_affiliation,
                title=entry.raw_title,
                notes=entry.notes,
                attached_company_name=attached_company_name,
                uploaded_at=entry.created_at,
            )
        )

    full_name = (
        (participant.full_name or "").strip()
        or " ".join([part for part in [participant.first_name, participant.last_name] if part]).strip()
        or participant.primary_email
    )

    return ParticipantProfileResponse(
        id=participant.id,
        first_name=participant.first_name,
        last_name=participant.last_name,
        full_name=full_name,
        email=participant.primary_email,
        linkedin_url=participant.linkedin_url,
        affiliations=affiliations,
        team_names=team_names,
        attached_company_names=attached_company_names,
        team_record_count=len(participant_rows),
        cohort_count=len({row_event.id for _, row_event in participant_rows}),
        associated_events=associated_events,
        created_at=participant.created_at,
    )


@router.get("/{participant_entry_id}", response_model=ParticipantEntryResponse)
def get_participant_entry(
    participant_entry_id: UUID,
    db: Session = Depends(get_db),
):
    entry = db.query(ParticipantListEntry).filter(ParticipantListEntry.id == participant_entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Participant entry not found")
    return _serialize_participant_entry(entry, db, _get_company_name_column(db))
