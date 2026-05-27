from datetime import date, datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func, inspect as sa_inspect, or_
from sqlalchemy.orm import Session

from app.api.endpoints.users import get_current_user
from app.core.db.session import get_db
from app.models.company import Company, ParticipantCompanyMatch
from app.models.events import Event, EventUniversity
from app.models.funding import FundingRecord
from app.models.patents import Patent
from app.models.saved_company import SavedCompany, SavedHubCompanyNote
from app.models.participant_lists import ParticipantList
from app.models.participants import ParticipantListEntry, Participants
from app.models.sam_entity_public_v2 import SamEntityPublicV2
from app.models.users import User
from app.services.company_enrichment import enrich_companies_in_bulk, enrich_company_profile, normalize_enrichment_sources

router = APIRouter(prefix="/company", tags=["companies"])


class CompanyCreate(BaseModel):
    name: str = Field(..., min_length=1)
    uei: Optional[str] = None
    duns: Optional[str] = None
    website_url: Optional[str] = None
    description: Optional[str] = None
    hq_city: Optional[str] = None
    hq_state: Optional[str] = None
    hq_country: Optional[str] = None


class CompanyResponse(BaseModel):
    id: UUID
    name: str
    uei: Optional[str] = None
    duns: Optional[str] = None
    website_url: Optional[str] = None
    description: Optional[str] = None
    hq_city: Optional[str] = None
    hq_state: Optional[str] = None
    hq_country: Optional[str] = None
    number_employees: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class CompanySearchResponse(BaseModel):
    companies: List[CompanyResponse]
    total: int


class SavedCompanyRequest(BaseModel):
    hub_id: UUID
    company_id: UUID
    notes: Optional[str] = None


class SavedCompanyResponse(BaseModel):
    id: UUID
    hub_id: UUID
    company_id: UUID
    notes: Optional[str] = None
    saved_at: datetime

    class Config:
        from_attributes = True


class RemoveSavedCompanyResponse(BaseModel):
    company_id: UUID
    removed: bool


class FundingRecordItemResponse(BaseModel):
    id: UUID
    date_awarded: Optional[date] = None
    amount_awarded: Optional[float] = None
    funding_source: Optional[str] = None
    funding_stage: Optional[str] = None
    award_end_month: Optional[int] = None
    award_end_year: Optional[int] = None
    award_number: Optional[str] = None
    investors: Optional[str] = None
    additional_info: Optional[str] = None
    award_title: Optional[str] = None
    source_url: Optional[str] = None


class CompanyIntelligenceResponse(BaseModel):
    company_id: UUID
    federal_funding: Optional[dict] = None
    sbir_awards: Optional[dict] = None
    patents: Optional[dict] = None
    funding_records: List[FundingRecordItemResponse] = Field(default_factory=list)
    total_funding_received: float = 0.0
    funding_received_post_icorps: float = 0.0


class SaveMatchesRequest(BaseModel):
    participant_list_id: UUID
    save_mode: str


class SaveMatchesResponse(BaseModel):
    saved: int


class CompanyEnrichmentRequest(BaseModel):
    sources: List[str] = Field(default_factory=list)


class CompanyEnrichmentResponse(BaseModel):
    company_id: UUID
    requested_sources: List[str]
    skipped_sources: List[str] = Field(default_factory=list)
    funding_records_processed: int = 0
    patents_processed: int = 0
    status: str
    message: str


class DashboardChartPoint(BaseModel):
    year: int
    count: int


class DashboardSummaryResponse(BaseModel):
    available_years: List[int]
    selected_year: Optional[int] = None
    events_count: int
    participants_count: int
    companies_saved_count: int
    incorporation_series: List[DashboardChartPoint]


def _apply_company_search_filters(query, search: Optional[str]):
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Company.company_name.ilike(term),
                Company.legal_name.ilike(term),
                Company.uei.ilike(term),
                Company.duns.ilike(term),
                Company.website_url.ilike(term),
                Company.city.ilike(term),
                Company.state_province.ilike(term),
                Company.hq_city.ilike(term),
                Company.hq_state.ilike(term),
            )
        )
    return query


def _predict_company_category(company: Company, sam_entity: Optional[SamEntityPublicV2]) -> tuple[Optional[str], Optional[float]]:
    combined_text = " ".join(
        value for value in [company.name, company.description, sam_entity.primary_naics if sam_entity else None]
        if value
    ).lower()

    category_rules = [
        ("Life Sciences / Biotech", ["bio", "therapeutic", "medical", "clinical", "pharma", "immun"], 0.92),
        ("AI / Software", ["software", "ai", "data", "platform", "saas", "analytics"], 0.9),
        ("Energy / Climate", ["energy", "climate", "power", "renewable", "water"], 0.89),
        ("Advanced Manufacturing", ["manufacturing", "robot", "hardware", "materials"], 0.87),
        ("GovTech / Defense", ["defense", "federal", "security", "government"], 0.86),
    ]

    for category, keywords, confidence in category_rules:
        if any(keyword in combined_text for keyword in keywords):
            return category, confidence

    if sam_entity and sam_entity.primary_naics:
        return f"NAICS {sam_entity.primary_naics}", 0.7

    return None, None


def _serialize_funding_record(record: FundingRecord) -> FundingRecordItemResponse:
    amount_value = record.amount_usd or record.award_amount
    return FundingRecordItemResponse(
        id=record.id,
        date_awarded=record.date_awarded,
        amount_awarded=float(amount_value) if amount_value is not None else None,
        funding_source=record.funding_source or record.funding_type,
        funding_stage=record.funding_stage or record.program_name,
        award_end_month=record.award_end_month,
        award_end_year=record.award_end_year,
        award_number=record.award_number,
        investors=record.investors,
        additional_info=record.additional_info,
        award_title=record.award_title,
        source_url=record.source_url or record.award_link,
    )


class CompanyAssociatedParticipantResponse(BaseModel):
    participant_id: Optional[UUID] = None
    full_name: str
    email: Optional[str] = None
    affiliation: Optional[str] = None
    team_name: Optional[str] = None
    cohort_id: Optional[UUID] = None
    cohort_name: Optional[str] = None


class CompanyAssociatedCohortResponse(BaseModel):
    cohort_id: UUID
    cohort_name: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


class CompanyProfileResponse(CompanyResponse):
    participant_count: int = 0
    cohort_count: int = 0
    is_saved: bool = False
    notes_from_partners: Optional[str] = None
    total_funding_received: float = 0.0
    funding_received_post_icorps: float = 0.0
    predicted_category: Optional[str] = None
    confidence_score: Optional[float] = None
    legal_name: Optional[str] = None
    cage: Optional[str] = None
    company_exists: bool = True
    active: Optional[bool] = None
    exit_date: Optional[str] = None
    company_website: Optional[str] = None
    employees: Optional[int] = None
    street_address: Optional[str] = None
    zip_code: Optional[str] = None
    legislative_district: Optional[str] = None
    industry: Optional[str] = None
    annual_revenue_range: Optional[str] = None
    merged: Optional[bool] = None
    merger_additional_info: Optional[str] = None
    incorporation_date: Optional[str] = None
    incorporation_year: Optional[int] = None
    incorporation_state: Optional[str] = None
    incorporation_country: Optional[str] = None
    incorporation_type: Optional[str] = None
    funding_records: List[FundingRecordItemResponse] = Field(default_factory=list)
    associated_participants: List[CompanyAssociatedParticipantResponse] = Field(default_factory=list)
    associated_cohorts: List[CompanyAssociatedCohortResponse] = Field(default_factory=list)


@router.post("/", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
def create_company(request: CompanyCreate, db: Session = Depends(get_db)):
    company = Company(
        name=request.name,
        uei=request.uei,
        duns=request.duns,
        website_url=request.website_url,
        description=request.description,
        hq_city=request.hq_city,
        hq_state=request.hq_state,
        hq_country=request.hq_country,
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.get("/", response_model=CompanySearchResponse)
def list_companies(
    search: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = _apply_company_search_filters(db.query(Company), search)

    total = query.count()
    companies = (
        query.order_by(Company.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return CompanySearchResponse(companies=companies, total=total)


@router.get("/saved", response_model=CompanySearchResponse)
def list_saved_companies(
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.hub_id:
        return CompanySearchResponse(companies=[], total=0)

    query = (
        db.query(Company)
        .join(SavedCompany, SavedCompany.company_id == Company.id)
        .filter(SavedCompany.hub_organization_id == current_user.hub_id)
    )
    query = _apply_company_search_filters(query, search)

    total = query.count()
    companies = (
        query.order_by(Company.name.asc(), Company.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return CompanySearchResponse(companies=companies, total=total)


@router.get("/search", response_model=CompanySearchResponse)
def search_company(
    uei: Optional[str] = Query(None),
    duns: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    if not uei and not duns and not name:
        raise HTTPException(status_code=400, detail="Provide uei, duns, or name")

    query = db.query(Company)
    if uei:
        query = query.filter(Company.uei.ilike(f"%{uei}%"))
    if duns:
        query = query.filter(Company.duns.ilike(f"%{duns}%"))
    if name:
        query = query.filter(Company.name.ilike(f"%{name}%"))

    companies = query.limit(100).all()
    return CompanySearchResponse(companies=companies, total=len(companies))


@router.get("/dashboard-summary", response_model=DashboardSummaryResponse)
def get_dashboard_summary(
    participation_year: Optional[int] = Query(None),
    scope: str = Query("hub"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    hub_id = current_user.hub_id
    if not hub_id:
        raise HTTPException(
            status_code=400,
            detail="Complete your profile with an affiliated institution to view dashboard metrics.",
        )

    if scope not in {"hub", "institution"}:
        raise HTTPException(status_code=400, detail="Invalid dashboard filter scope.")

    if scope == "institution" and not current_user.university_id:
        raise HTTPException(
            status_code=400,
            detail="Add your university to use the institution-hosted events filter.",
        )

    company_columns = {
        column["name"]
        for column in sa_inspect(db.bind).get_columns("companies")
    }
    company_year_source = (
        Company.incorporation_date
        if "incorporation_date" in company_columns
        else Company.created_at
    )

    def build_event_scope_subquery(include_year: bool = False):
        query = db.query(Event.id).filter(Event.hub_organization_id == hub_id)

        if scope == "institution":
            query = (
                query.outerjoin(EventUniversity, EventUniversity.event_id == Event.id)
                .filter(
                    or_(
                        Event.lead_university_id == current_user.university_id,
                        EventUniversity.university_id == current_user.university_id,
                    )
                )
            )

        if include_year and participation_year is not None:
            query = query.filter(sa_func.extract("year", Event.start_date) == participation_year)

        return query.distinct().subquery()

    scoped_event_ids = build_event_scope_subquery(include_year=False)
    filtered_event_ids = build_event_scope_subquery(include_year=True)

    event_year_rows = (
        db.query(sa_func.extract("year", Event.start_date))
        .join(scoped_event_ids, Event.id == scoped_event_ids.c.id)
        .filter(Event.start_date.isnot(None))
        .all()
    )
    available_years = sorted(
        {int(year) for (year,) in event_year_rows if year is not None},
        reverse=True,
    )

    event_count_query = db.query(sa_func.count()).select_from(filtered_event_ids)
    participant_count_query = (
        db.query(sa_func.count(ParticipantListEntry.id))
        .join(ParticipantList, ParticipantListEntry.participant_list_id == ParticipantList.id)
        .join(filtered_event_ids, ParticipantList.event_id == filtered_event_ids.c.id)
    )

    if scope == "institution":
        saved_companies_query = (
            db.query(sa_func.count(sa_func.distinct(SavedCompany.id)))
            .join(ParticipantCompanyMatch, ParticipantCompanyMatch.company_id == SavedCompany.company_id)
            .join(ParticipantListEntry, ParticipantCompanyMatch.participant_list_entry_id == ParticipantListEntry.id)
            .join(ParticipantList, ParticipantListEntry.participant_list_id == ParticipantList.id)
            .join(filtered_event_ids, ParticipantList.event_id == filtered_event_ids.c.id)
            .filter(SavedCompany.hub_organization_id == hub_id)
        )
        incorporation_query = (
            db.query(
                sa_func.extract("year", company_year_source).label("year"),
                sa_func.count(sa_func.distinct(SavedCompany.id)).label("count"),
            )
            .join(SavedCompany, SavedCompany.company_id == Company.id)
            .join(ParticipantCompanyMatch, ParticipantCompanyMatch.company_id == SavedCompany.company_id)
            .join(ParticipantListEntry, ParticipantCompanyMatch.participant_list_entry_id == ParticipantListEntry.id)
            .join(ParticipantList, ParticipantListEntry.participant_list_id == ParticipantList.id)
            .join(filtered_event_ids, ParticipantList.event_id == filtered_event_ids.c.id)
            .filter(
                SavedCompany.hub_organization_id == hub_id,
                company_year_source.isnot(None),
            )
        )
    else:
        saved_companies_query = db.query(sa_func.count(SavedCompany.id)).filter(
            SavedCompany.hub_organization_id == hub_id
        )
        incorporation_query = (
            db.query(
                sa_func.extract("year", company_year_source).label("year"),
                sa_func.count(SavedCompany.id).label("count"),
            )
            .join(SavedCompany, SavedCompany.company_id == Company.id)
            .filter(
                SavedCompany.hub_organization_id == hub_id,
                company_year_source.isnot(None),
            )
        )

        if participation_year is not None:
            saved_companies_query = saved_companies_query.filter(
                sa_func.extract("year", SavedCompany.created_at) == participation_year
            )
            incorporation_query = incorporation_query.filter(
                sa_func.extract("year", SavedCompany.created_at) == participation_year
            )

    incorporation_rows = incorporation_query.group_by("year").order_by("year").all()

    return DashboardSummaryResponse(
        available_years=available_years,
        selected_year=participation_year,
        events_count=int(event_count_query.scalar() or 0),
        participants_count=int(participant_count_query.scalar() or 0),
        companies_saved_count=int(saved_companies_query.scalar() or 0),
        incorporation_series=[
            DashboardChartPoint(year=int(row.year), count=int(row.count or 0))
            for row in incorporation_rows
            if row.year is not None
        ],
    )


@router.get("/{company_id}/profile", response_model=CompanyProfileResponse)
def get_company_profile(
    company_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    match_rows = (
        db.query(
            ParticipantListEntry.participant_id.label("participant_id"),
            ParticipantListEntry.raw_full_name.label("raw_full_name"),
            ParticipantListEntry.raw_email.label("raw_email"),
            ParticipantListEntry.raw_affiliation.label("raw_affiliation"),
            ParticipantListEntry.raw_team_name.label("raw_team_name"),
            Participants.first_name.label("first_name"),
            Participants.last_name.label("last_name"),
            Participants.primary_email.label("primary_email"),
            Participants._full_name.label("full_name"),
            Event.id.label("event_id"),
            Event.name.label("event_name"),
            Event.start_date.label("start_date"),
            Event.end_date.label("end_date"),
        )
        .select_from(Company)
        .join(
            ParticipantCompanyMatch,
            ParticipantCompanyMatch.company_id == Company.id,
        )
        .join(
            ParticipantListEntry,
            ParticipantListEntry.id == ParticipantCompanyMatch.participant_list_entry_id,
        )
        .join(ParticipantList, ParticipantList.id == ParticipantListEntry.participant_list_id)
        .join(Event, Event.id == ParticipantList.event_id)
        .outerjoin(Participants, Participants.id == ParticipantListEntry.participant_id)
        .filter(
            Company.id == company_id,
            ParticipantCompanyMatch.is_active.is_(True),
        )
        .order_by(Event.start_date.desc(), Event.name.asc())
        .all()
    )

    associated_participants: List[CompanyAssociatedParticipantResponse] = []
    associated_cohorts: List[CompanyAssociatedCohortResponse] = []
    seen_participants = set()
    seen_cohorts = set()

    saved_record = None
    latest_note = None
    if current_user.hub_id:
        saved_record = (
            db.query(SavedCompany)
            .filter(
                SavedCompany.hub_organization_id == current_user.hub_id,
                SavedCompany.company_id == company_id,
            )
            .first()
        )
        if saved_record:
            latest_note = (
                db.query(SavedHubCompanyNote)
                .filter(SavedHubCompanyNote.hub_favorite_company_id == saved_record.id)
                .order_by(SavedHubCompanyNote.created_at.desc())
                .first()
            )

    best_match = (
        db.query(ParticipantCompanyMatch)
        .filter(
            ParticipantCompanyMatch.company_id == company_id,
            ParticipantCompanyMatch.is_active.is_(True),
        )
        .order_by(ParticipantCompanyMatch.confidence.desc())
        .first()
    )

    sam_entity = None
    if company.uei:
        sam_entity = db.query(SamEntityPublicV2).filter(SamEntityPublicV2.uei == company.uei).first()

    funding_records = (
        db.query(FundingRecord)
        .filter(FundingRecord.company_id == company_id)
        .order_by(FundingRecord.date_awarded.desc().nullslast(), FundingRecord.created_at.desc())
        .all()
    )
    serialized_funding_records = [_serialize_funding_record(record) for record in funding_records]
    total_funding_received = sum(
        float(record.amount_usd or record.award_amount or 0)
        for record in funding_records
    )

    predicted_category, predicted_confidence = _predict_company_category(company, sam_entity)
    confidence_score = float(best_match.confidence) if best_match and best_match.confidence is not None else predicted_confidence

    street_parts = [company.address1, company.address2]
    if sam_entity and not any(street_parts):
        street_parts = [sam_entity.address_line1, sam_entity.address_line2]
    street_address = ", ".join(part for part in street_parts if part) or None

    active_value = None
    if sam_entity is not None:
        active_value = bool(sam_entity.is_active) if sam_entity.is_active is not None else (sam_entity.registration_status or "").upper() == "A"

    for row in match_rows:
        participant_name = (
            row.full_name
            or " ".join(part for part in [row.first_name, row.last_name] if part).strip()
            or row.raw_full_name
            or row.primary_email
            or row.raw_email
            or "Unknown Participant"
        )
        participant_email = row.primary_email or row.raw_email
        participant_key = str(row.participant_id or participant_email or participant_name)

        if participant_key not in seen_participants:
            seen_participants.add(participant_key)
            associated_participants.append(
                CompanyAssociatedParticipantResponse(
                    participant_id=row.participant_id,
                    full_name=participant_name,
                    email=participant_email,
                    affiliation=row.raw_affiliation,
                    team_name=row.raw_team_name,
                    cohort_id=row.event_id,
                    cohort_name=row.event_name,
                )
            )

        if row.event_id and row.event_id not in seen_cohorts:
            seen_cohorts.add(row.event_id)
            associated_cohorts.append(
                CompanyAssociatedCohortResponse(
                    cohort_id=row.event_id,
                    cohort_name=row.event_name or "Unnamed Cohort",
                    start_date=row.start_date,
                    end_date=row.end_date,
                )
            )

    cohort_dates = [
        ((cohort.end_date or cohort.start_date).date() if hasattr((cohort.end_date or cohort.start_date), "date") else (cohort.end_date or cohort.start_date))
        for cohort in associated_cohorts
        if cohort.end_date or cohort.start_date
    ]
    icorps_cutoff = min(cohort_dates, default=None)
    funding_received_post_icorps = sum(
        float(record.amount_usd or record.award_amount or 0)
        for record in funding_records
        if not icorps_cutoff or not record.date_awarded or record.date_awarded >= icorps_cutoff
    )

    return CompanyProfileResponse(
        id=company.id,
        name=company.name,
        uei=company.uei,
        duns=company.duns,
        website_url=company.website_url,
        description=company.description,
        hq_city=company.hq_city or company.city,
        hq_state=company.hq_state or company.state,
        hq_country=company.hq_country or (sam_entity.country if sam_entity else None),
        number_employees=company.number_employees,
        created_at=company.created_at,
        participant_count=len(associated_participants),
        cohort_count=len(associated_cohorts),
        is_saved=bool(saved_record),
        notes_from_partners=latest_note.note if latest_note else None,
        total_funding_received=total_funding_received,
        funding_received_post_icorps=funding_received_post_icorps,
        predicted_category=predicted_category,
        confidence_score=confidence_score,
        legal_name=company.name,
        cage=sam_entity.cage_code if sam_entity else None,
        company_exists=True,
        active=active_value,
        exit_date=None,
        company_website=company.website_url or company.company_url or (sam_entity.website if sam_entity else None),
        employees=company.number_employees,
        street_address=street_address,
        zip_code=company.zip or (sam_entity.zip_code if sam_entity else None),
        legislative_district=sam_entity.congressional_district if sam_entity else None,
        industry=(sam_entity.primary_naics if sam_entity and sam_entity.primary_naics else predicted_category),
        annual_revenue_range=None,
        merged=False,
        merger_additional_info=None,
        incorporation_date=None,
        incorporation_year=None,
        incorporation_state=sam_entity.incorporation_state if sam_entity else None,
        incorporation_country=sam_entity.incorporation_country if sam_entity else None,
        incorporation_type=None,
        funding_records=serialized_funding_records,
        associated_participants=associated_participants,
        associated_cohorts=associated_cohorts,
    )


@router.post("/{company_id}/enrich", response_model=CompanyEnrichmentResponse)
def run_company_enrichment(
    company_id: UUID,
    request: CompanyEnrichmentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    del current_user
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    requested_sources = normalize_enrichment_sources(request.sources)
    summary = enrich_company_profile(company_id, requested_sources)
    if summary.get("status") == "failed":
        raise HTTPException(status_code=502, detail="Company enrichment failed")

    return CompanyEnrichmentResponse(
        company_id=company_id,
        requested_sources=summary.get("requested_sources", requested_sources),
        skipped_sources=summary.get("skipped_sources", []),
        funding_records_processed=summary.get("funding_records_processed", 0),
        patents_processed=summary.get("patents_processed", 0),
        status=summary.get("status", "completed"),
        message=summary.get("message", "Company enrichment completed."),
    )


@router.get("/{company_id}", response_model=CompanyResponse)
def get_company(company_id: UUID, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.post("/save", response_model=SavedCompanyResponse, status_code=status.HTTP_201_CREATED)
def save_company(
    request: SavedCompanyRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    saved = (
        db.query(SavedCompany)
        .filter(
            SavedCompany.hub_organization_id == request.hub_id,
            SavedCompany.company_id == request.company_id,
        )
        .first()
    )

    if not saved:
        saved = SavedCompany(
            hub_organization_id=request.hub_id,
            company_id=request.company_id,
        )
        db.add(saved)
        db.flush()

    if request.notes:
        db.add(
            SavedHubCompanyNote(
                hub_favorite_company_id=saved.id,
                note=request.notes,
            )
        )

    db.commit()
    db.refresh(saved)
    background_tasks.add_task(enrich_company_profile, request.company_id)

    latest_note = (
        db.query(SavedHubCompanyNote)
        .filter(SavedHubCompanyNote.hub_favorite_company_id == saved.id)
        .order_by(SavedHubCompanyNote.created_at.desc())
        .first()
    )
    return SavedCompanyResponse(
        id=saved.id,
        hub_id=saved.hub_organization_id,
        company_id=saved.company_id,
        notes=latest_note.note if latest_note else None,
        saved_at=saved.saved_at,
    )


@router.delete("/saved/{company_id}", response_model=RemoveSavedCompanyResponse)
def remove_saved_company(
    company_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.hub_id:
        raise HTTPException(status_code=400, detail="Complete your profile with a hub affiliation first.")

    saved = (
        db.query(SavedCompany)
        .filter(
            SavedCompany.hub_organization_id == current_user.hub_id,
            SavedCompany.company_id == company_id,
        )
        .first()
    )
    if not saved:
        raise HTTPException(status_code=404, detail="Saved company not found")

    db.query(SavedHubCompanyNote).filter(
        SavedHubCompanyNote.hub_favorite_company_id == saved.id,
    ).delete(synchronize_session=False)
    db.delete(saved)
    db.commit()
    return RemoveSavedCompanyResponse(company_id=company_id, removed=True)


@router.get("/{company_id}/intelligence", response_model=CompanyIntelligenceResponse)
def get_company_intelligence(company_id: UUID, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    funding_records = db.query(FundingRecord).filter(FundingRecord.company_id == company_id).all()
    patents = db.query(Patent).filter(Patent.company_id == company_id).all()

    total_awarded = 0
    agencies = set()
    for record in funding_records:
        if record.amount_usd:
            total_awarded += float(record.amount_usd)
        elif record.award_amount:
            total_awarded += float(record.award_amount)
        if record.agency:
            agencies.add(record.agency)

    funding_item_rows = [_serialize_funding_record(record) for record in funding_records]

    icorps_dates = (
        db.query(Event.start_date)
        .join(ParticipantList, ParticipantList.event_id == Event.id)
        .join(ParticipantListEntry, ParticipantListEntry.participant_list_id == ParticipantList.id)
        .join(ParticipantCompanyMatch, ParticipantCompanyMatch.participant_list_entry_id == ParticipantListEntry.id)
        .filter(
            ParticipantCompanyMatch.company_id == company_id,
            ParticipantCompanyMatch.is_active.is_(True),
            Event.start_date.isnot(None),
        )
        .all()
    )
    icorps_cutoff = min(
        (
            date_value.date() if hasattr(date_value, "date") else date_value
            for (date_value,) in icorps_dates
            if date_value is not None
        ),
        default=None,
    )
    funding_received_post_icorps = sum(
        float(record.amount_usd or record.award_amount or 0)
        for record in funding_records
        if not icorps_cutoff or not record.date_awarded or record.date_awarded >= icorps_cutoff
    )

    federal_funding = {
        "total_awarded": total_awarded,
        "award_count": len(funding_records),
        "agencies": sorted(agencies),
    }

    patent_summary = {
        "total_patents": len(patents),
        "active_patents": len([patent for patent in patents if (patent.status or "").lower() == "active"]),
        "patent_list": [
            {
                "title": patent.title,
                "patent_number": patent.patent_number,
                "filing_date": patent.filing_date,
            }
            for patent in patents[:50]
        ],
    }

    return CompanyIntelligenceResponse(
        company_id=company_id,
        federal_funding=federal_funding,
        sbir_awards=None,
        patents=patent_summary,
        funding_records=funding_item_rows,
        total_funding_received=total_awarded,
        funding_received_post_icorps=funding_received_post_icorps,
    )


@router.post("/save-bulk", response_model=SaveMatchesResponse)
def save_bulk_matches(
    request: SaveMatchesRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if request.save_mode not in {"all", "very_high", "very_high_high"}:
        raise HTTPException(status_code=400, detail="Invalid save_mode")

    participant_list = (
        db.query(ParticipantList)
        .filter(ParticipantList.id == request.participant_list_id)
        .first()
    )
    if not participant_list:
        raise HTTPException(status_code=404, detail="Participant list not found")

    event = db.query(Event).filter(Event.id == participant_list.event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    entry_ids = (
        db.query(ParticipantListEntry.id)
        .filter(ParticipantListEntry.participant_list_id == request.participant_list_id)
        .all()
    )
    entry_ids = [entry_id for (entry_id,) in entry_ids]

    matches = (
        db.query(ParticipantCompanyMatch)
        .filter(
            ParticipantCompanyMatch.participant_list_entry_id.in_(entry_ids),
            ParticipantCompanyMatch.is_active.is_(True),
        )
        .all()
    )

    def allowed(confidence: float) -> bool:
        if confidence >= 0.9:
            return True
        if request.save_mode == "very_high":
            return False
        if confidence >= 0.75:
            return True
        if request.save_mode == "very_high_high":
            return False
        return confidence >= 0.6

    saved = 0
    saved_company_ids = set()
    for match in matches:
        confidence = float(match.confidence or 0)
        if not allowed(confidence):
            continue

        existing = (
            db.query(SavedCompany)
            .filter(
                SavedCompany.hub_organization_id == event.hub_organization_id,
                SavedCompany.company_id == match.company_id,
            )
            .first()
        )
        if existing:
            continue

        saved_record = SavedCompany(
            hub_organization_id=event.hub_organization_id,
            company_id=match.company_id,
        )
        db.add(saved_record)
        saved_company_ids.add(match.company_id)
        saved += 1

    db.commit()
    if saved_company_ids:
        background_tasks.add_task(enrich_companies_in_bulk, list(saved_company_ids))
    return SaveMatchesResponse(saved=saved)
