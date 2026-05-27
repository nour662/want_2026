from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel
from typing import List, Optional
from uuid import UUID
from app.core.db.session import get_db
from app.models.company import Company, ParticipantCompanyMatch
from app.models.participants import ParticipantListEntry, Participants


router = APIRouter(
    prefix="/company_search",
    tags=["company_search"],
)


class CompanySearchRequest(BaseModel):
    search_type: str
    query: Optional[str] = None
    queries: Optional[List[str]] = None
    participant_name: Optional[str] = None


class CompanyResponse(BaseModel):
    id: UUID
    name: str
    uei: Optional[str] = None
    duns: Optional[str] = None
    website_url: Optional[str] = None
    description: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    number_employees: Optional[int] = None

    class Config:
        from_attributes = True


class CompanySearchResponse(BaseModel):
    companies: List[CompanyResponse]
    total: int


@router.post("/", response_model=CompanySearchResponse)
def search_companies(request: CompanySearchRequest, db: Session = Depends(get_db)):
    query = db.query(Company)
    search_queries = [
        value.strip()
        for value in (request.queries if request.queries else ([request.query] if request.query else []))
        if value and value.strip()
    ]
    participant_name = (request.participant_name or "").strip()

    if request.search_type not in {"uei", "duns", "name_participant"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid search_type. Must be 'uei', 'duns', or 'name_participant'",
        )

    if not search_queries and not participant_name:
        return CompanySearchResponse(companies=[], total=0)

    if request.search_type == "uei":
        if not search_queries:
            return CompanySearchResponse(companies=[], total=0)
        query = query.filter(or_(*[Company.uei.ilike(f"%{value}%") for value in search_queries]))

    elif request.search_type == "duns":
        if not search_queries:
            return CompanySearchResponse(companies=[], total=0)
        query = query.filter(or_(*[Company.duns.ilike(f"%{value}%") for value in search_queries]))

    else:
        if search_queries:
            query = query.filter(
                or_(
                    *[
                        or_(
                            Company.name.ilike(f"%{value}%"),
                            Company.normalized_name.ilike(f"%{value}%"),
                        )
                        for value in search_queries
                    ]
                )
            )

        if participant_name:
            participant_term = f"%{participant_name}%"
            query = (
                query.join(ParticipantCompanyMatch, ParticipantCompanyMatch.company_id == Company.id)
                .join(
                    ParticipantListEntry,
                    ParticipantListEntry.id == ParticipantCompanyMatch.participant_list_entry_id,
                )
                .outerjoin(Participants, Participants.id == ParticipantListEntry.participant_id)
                .filter(
                    or_(
                        Participants._full_name.ilike(participant_term),
                        Participants.first_name.ilike(participant_term),
                        Participants.last_name.ilike(participant_term),
                        ParticipantListEntry.raw_full_name.ilike(participant_term),
                    )
                )
            )

    companies = (
        query.distinct()
        .order_by(Company.name.asc(), Company.created_at.desc())
        .limit(100)
        .all()
    )
    return CompanySearchResponse(companies=companies, total=len(companies))
