from datetime import date, datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.endpoints.users import get_current_user
from app.core.db.session import get_db
from app.models.company import ParticipantCompanyMatch
from app.models.events import Event, EventUniversity
from app.models.match_jobs import MatchJob, MatchJobResult
from app.models.participant_lists import ParticipantList
from app.models.participants import ParticipantEntryUniversityLink, ParticipantListEntry
from app.models.university import University
from app.models.users import User

router = APIRouter(prefix="/events", tags=["events"])


class EventLocationType(str, Enum):
    IN_PERSON = "In Person"
    HYBRID = "Hybrid"
    VIRTUAL = "Virtual"


class EventCreate(BaseModel):
    hub_id: Optional[UUID] = None
    name: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    start_date: date
    end_date: date
    location: EventLocationType
    description: Optional[str] = None
    created_by_user_id: Optional[UUID] = None
    lead_university_id: UUID
    partner_institution_ids: List[UUID] = Field(default_factory=list)
    other_partner_institutions: List[str] = Field(default_factory=list)
    is_seven_week_program: bool = False


class EventResponse(BaseModel):
    id: UUID
    hub_id: UUID
    name: str
    event_type: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    location: Optional[EventLocationType] = None
    description: Optional[str] = None
    created_by_user_id: Optional[UUID] = None
    created_by_name: Optional[str] = None
    lead_university_id: Optional[UUID] = None
    partner_institution_ids: List[UUID] = Field(default_factory=list)
    other_partner_institutions: List[str] = Field(default_factory=list)
    is_seven_week_program: bool = False
    participant_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


class EventListResponse(BaseModel):
    events: List[EventResponse]
    total: int


def _normalize_custom_institutions(values: List[str]) -> List[str]:
    cleaned = []
    for value in values or []:
        normalized = (value or "").strip()
        if normalized:
            cleaned.append(normalized)
    return cleaned


def _serialize_event(event: Event, db: Session) -> EventResponse:
    university_links = (
        db.query(EventUniversity)
        .filter(EventUniversity.event_id == event.id)
        .order_by(EventUniversity.created_at.asc())
        .all()
    )
    partner_ids = [
        link.university_id
        for link in university_links
        if (link.involvement_role or "partner") == "partner"
    ]
    lead_link = next(
        (link.university_id for link in university_links if (link.involvement_role or "") == "lead"),
        None,
    )
    other_partners = [
        item.strip()
        for item in (event.partner_institutions_other or "").split("\n")
        if item.strip()
    ]

    # participant count across all lists for this event
    participant_count = (
        db.query(sa_func.count(ParticipantListEntry.id))
        .join(ParticipantList, ParticipantListEntry.participant_list_id == ParticipantList.id)
        .filter(ParticipantList.event_id == event.id)
        .scalar()
    ) or 0

    created_by_name: Optional[str] = None
    if event.created_by_user_id:
        creator = db.query(User).filter(User.id == event.created_by_user_id).first()
        if creator:
            created_by_name = (creator.full_name or "").strip() or creator.email

    return EventResponse(
        id=event.id,
        hub_id=event.hub_organization_id,
        name=event.name,
        event_type=event.event_type,
        start_date=event.start_date,
        end_date=event.end_date,
        location=event.location,
        description=event.description,
        created_by_user_id=event.created_by_user_id,
        created_by_name=created_by_name,
        lead_university_id=event.lead_university_id or lead_link,
        partner_institution_ids=partner_ids,
        other_partner_institutions=other_partners,
        is_seven_week_program=bool(event.is_seven_week_program),
        participant_count=participant_count,
        created_at=event.created_at,
    )


@router.post("/", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
def create_event(
    request: EventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if request.start_date and request.end_date and request.end_date < request.start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after the start date.")

    if not current_user.university_id:
        raise HTTPException(
            status_code=400,
            detail="Complete your profile with an affiliated institution before creating an event.",
        )

    affiliated_university = (
        db.query(University)
        .filter(University.id == current_user.university_id)
        .first()
    )
    if not affiliated_university or not affiliated_university.hub_id:
        raise HTTPException(
            status_code=400,
            detail="Your profile is missing a valid hub affiliation.",
        )

    hub_id = affiliated_university.hub_id
    hub_universities = db.query(University).filter(University.hub_id == hub_id).all()
    allowed_university_ids = {university.id for university in hub_universities}

    if request.lead_university_id not in allowed_university_ids:
        raise HTTPException(
            status_code=400,
            detail="Lead institution must be affiliated with your assigned hub.",
        )

    invalid_partner_ids = [
        university_id
        for university_id in request.partner_institution_ids
        if university_id not in allowed_university_ids
    ]
    if invalid_partner_ids:
        raise HTTPException(
            status_code=400,
            detail="Partner institutions must be affiliated with your assigned hub.",
        )

    custom_partner_names = _normalize_custom_institutions(request.other_partner_institutions)
    event = Event(
        hub_organization_id=hub_id,
        name=request.name.strip(),
        event_type=request.event_type,
        start_date=request.start_date,
        end_date=request.end_date,
        location=request.location,
        description=request.description or None,
        lead_university_id=request.lead_university_id,
        partner_institutions_other="\n".join(custom_partner_names) or None,
        is_seven_week_program=request.is_seven_week_program,
        created_by_user_id=current_user.id,
    )
    db.add(event)
    db.flush()

    db.add(
        EventUniversity(
            event_id=event.id,
            university_id=request.lead_university_id,
            involvement_role="lead",
        )
    )

    for university_id in sorted(set(request.partner_institution_ids)):
        if university_id == request.lead_university_id:
            continue
        db.add(
            EventUniversity(
                event_id=event.id,
                university_id=university_id,
                involvement_role="partner",
            )
        )

    db.commit()
    db.refresh(event)
    return _serialize_event(event, db)


@router.get("/", response_model=EventListResponse)
def list_events(hub_id: Optional[UUID] = None, db: Session = Depends(get_db)):
    query = db.query(Event)
    if hub_id:
        query = query.filter(Event.hub_organization_id == hub_id)
    events = query.order_by(Event.start_date.desc().nullslast()).all()
    response_events = [_serialize_event(event, db) for event in events]
    return EventListResponse(events=response_events, total=len(response_events))


@router.get("/{event_id}", response_model=EventResponse)
def get_event(event_id: UUID, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _serialize_event(event, db)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(
    event_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    try:
        participant_list_ids = [
            participant_list_id
            for (participant_list_id,) in db.query(ParticipantList.id)
            .filter(ParticipantList.event_id == event_id)
            .all()
        ]

        entry_ids = []
        if participant_list_ids:
            entry_ids = [
                entry_id
                for (entry_id,) in db.query(ParticipantListEntry.id)
                .filter(ParticipantListEntry.participant_list_id.in_(participant_list_ids))
                .all()
            ]

            match_job_ids = [
                match_job_id
                for (match_job_id,) in db.query(MatchJob.id)
                .filter(MatchJob.participant_list_id.in_(participant_list_ids))
                .all()
            ]

            if match_job_ids:
                db.query(MatchJobResult).filter(
                    MatchJobResult.match_job_id.in_(match_job_ids)
                ).delete(synchronize_session=False)

            if entry_ids:
                db.query(MatchJobResult).filter(
                    MatchJobResult.participant_list_entry_id.in_(entry_ids)
                ).delete(synchronize_session=False)
                db.query(ParticipantCompanyMatch).filter(
                    ParticipantCompanyMatch.participant_list_entry_id.in_(entry_ids)
                ).delete(synchronize_session=False)
                db.query(ParticipantEntryUniversityLink).filter(
                    ParticipantEntryUniversityLink.participant_list_entry_id.in_(entry_ids)
                ).delete(synchronize_session=False)
                db.query(ParticipantListEntry).filter(
                    ParticipantListEntry.id.in_(entry_ids)
                ).delete(synchronize_session=False)

            db.query(MatchJob).filter(
                MatchJob.participant_list_id.in_(participant_list_ids)
            ).delete(synchronize_session=False)
            db.query(ParticipantList).filter(
                ParticipantList.id.in_(participant_list_ids)
            ).delete(synchronize_session=False)

        db.query(EventUniversity).filter(
            EventUniversity.event_id == event_id
        ).delete(synchronize_session=False)
        db.query(Event).filter(Event.id == event_id).delete(synchronize_session=False)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Failed to delete the event and its related records.",
        ) from exc
