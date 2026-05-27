import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import List, Optional
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.models.company import Company, ParticipantCompanyMatch
from app.models.match_jobs import MatchJob
from app.models.participant_lists import ParticipantList
from app.models.participants import ParticipantListEntry
from app.models.sam_entity_public_v2 import SamEntityPublicV2

router = APIRouter(prefix="/matching", tags=["matching"])


class MatchingRequest(BaseModel):
    participant_list_id: UUID
    initiated_by_user_id: UUID
    job_type: Optional[str] = "company_matching"


class MatchingResponse(BaseModel):
    job_id: UUID
    status: str
    created_at: datetime
    participant_list_id: UUID
    job_type: str


class MatchJobStatusResponse(BaseModel):
    job_id: UUID
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class MatchCompanyResponse(BaseModel):
    id: UUID
    name: str
    uei: Optional[str] = None
    duns: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None


class MatchParticipantResponse(BaseModel):
    id: UUID
    team_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None


class MatchResultResponse(BaseModel):
    match_id: UUID
    participant: MatchParticipantResponse
    company: Optional[MatchCompanyResponse] = None
    confidence: float
    match_level: str
    match_type: str
    match_source: str
    sam_profile: Optional[dict] = None
    rationale: Optional[dict] = None


class MatchResultsResponse(BaseModel):
    results: List[MatchResultResponse]
    total: int


class RemoveMatchResponse(BaseModel):
    match_id: UUID
    removed: bool


COMPANY_SUFFIXES = {
    "inc",
    "llc",
    "ltd",
    "corp",
    "co",
    "company",
    "pllc",
    "lp",
    "llp",
    "incorporated",
    "corporation",
}
FREE_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}
WEIGHT_COMPANY_NAME = 0.65
WEIGHT_WEBSITE = 0.15
WEIGHT_CONTACT = 0.20


def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _normalize_company_name(name: Optional[str]) -> str:
    tokens = _normalize_text(name).split()
    while tokens and tokens[-1] in COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _similarity(left: Optional[str], right: Optional[str]) -> float:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _extract_domain(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    raw_value = value.strip().lower()
    if not raw_value:
        return None

    if "@" in raw_value and " " not in raw_value:
        domain = raw_value.split("@", 1)[1]
    else:
        parsed = urlparse(raw_value if "://" in raw_value else f"https://{raw_value}")
        domain = parsed.netloc or parsed.path.split("/")[0]

    domain = domain.split(":", 1)[0].removeprefix("www.")
    return domain or None


def _extract_contact_names(raw_fields: Optional[dict]) -> List[str]:
    names = set()

    def walk(value, parent_key: str = ""):
        if isinstance(value, dict):
            for key, nested_value in value.items():
                next_parent = f"{parent_key}.{key}" if parent_key else str(key)
                walk(nested_value, next_parent)
        elif isinstance(value, list):
            for item in value:
                walk(item, parent_key)
        elif isinstance(value, str):
            normalized = _normalize_text(value)
            key = parent_key.lower()
            if not normalized:
                return
            if any(token in key for token in ("contact", "poc", "pointofcontact", "administrator", "owner")) and len(normalized.split()) >= 2:
                names.add(normalized)

    walk(raw_fields or {})
    return sorted(names)


def _score_candidate(
    company_name: Optional[str],
    website_domain: Optional[str],
    participant_names: List[str],
    sam_entity: SamEntityPublicV2,
    exact_uei_match: bool = False,
) -> tuple[float, dict]:
    candidate_name = sam_entity.legal_business_name or sam_entity.dba_name or ""
    name_score = _similarity(_normalize_company_name(company_name), _normalize_company_name(candidate_name))

    candidate_domain = _extract_domain(sam_entity.website)
    website_score = _similarity(website_domain, candidate_domain) if website_domain and candidate_domain else 0.0

    contact_names = _extract_contact_names(sam_entity.raw_fields)
    contact_score = 0.0
    if participant_names and contact_names:
        contact_score = max(
            _similarity(participant_name, contact_name)
            for participant_name in participant_names
            for contact_name in contact_names
        )

    weighted_sum = 0.0
    available_weight = 0.0

    if company_name:
        weighted_sum += name_score * WEIGHT_COMPANY_NAME
        available_weight += WEIGHT_COMPANY_NAME
    if website_domain and candidate_domain:
        weighted_sum += website_score * WEIGHT_WEBSITE
        available_weight += WEIGHT_WEBSITE
    if participant_names and contact_names:
        weighted_sum += contact_score * WEIGHT_CONTACT
        available_weight += WEIGHT_CONTACT

    weighted_score = (weighted_sum / available_weight) if available_weight else 0.0
    if exact_uei_match:
        weighted_score = max(weighted_score, 0.98)

    return weighted_score, {
        "company_name_similarity": round(name_score, 3),
        "website_similarity": round(website_score, 3),
        "contact_similarity": round(contact_score, 3),
        "weighted_score": round(weighted_score, 3),
        "candidate_uei": sam_entity.uei,
        "candidate_name": candidate_name,
        "exact_uei_match": exact_uei_match,
    }


def _upsert_marker(notes: Optional[str], label: str, value: Optional[str]) -> Optional[str]:
    if not value:
        return notes

    marker = f"{label}:"
    parts = []
    replaced = False
    for part in (notes or "").split("|"):
        segment = part.strip()
        if not segment:
            continue
        if segment.lower().startswith(marker.lower()):
            parts.append(f"{label}: {value}")
            replaced = True
        else:
            parts.append(segment)

    if not replaced:
        parts.append(f"{label}: {value}")

    return " | ".join(parts) if parts else None


def _match_level(confidence: float) -> str:
    if confidence >= 0.9:
        return "very_high"
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


def _extract_marker(notes: Optional[str], label: str) -> Optional[str]:
    if not notes:
        return None
    marker = f"{label}:"
    if marker not in notes:
        return None
    for part in notes.split("|"):
        segment = part.strip()
        if segment.lower().startswith(marker.lower()):
            value = segment.split(":", 1)[1].strip()
            return value or None
    return None


def _get_or_create_company(db: Session, sam_entity: SamEntityPublicV2) -> Company:
    company = None
    if sam_entity.uei:
        company = db.query(Company).filter(Company.uei == sam_entity.uei).first()
    if not company:
        company = db.query(Company).filter(Company.name == sam_entity.legal_business_name).first()
    if company:
        return company
    company = Company(
        name=sam_entity.legal_business_name,
        uei=sam_entity.uei,
        duns=sam_entity.cage_code,
        city=sam_entity.city,
        state=sam_entity.state,
        hq_country=sam_entity.country,
        website_url=sam_entity.website,
        domain=_extract_domain(sam_entity.website),
    )
    db.add(company)
    db.flush()
    return company


@router.post("/run", response_model=MatchingResponse, status_code=status.HTTP_202_ACCEPTED)
def run_matching(request: MatchingRequest, db: Session = Depends(get_db)):
    participant_list = (
        db.query(ParticipantList)
        .filter(ParticipantList.id == request.participant_list_id)
        .first()
    )
    if not participant_list:
        raise HTTPException(status_code=404, detail="Participant list not found")

    job = MatchJob(
        participant_list_id=request.participant_list_id,
        initiated_by_user_id=request.initiated_by_user_id,
        job_type=request.job_type or "company_matching",
        status="running",
        started_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    entries = (
        db.query(ParticipantListEntry)
        .filter(ParticipantListEntry.participant_list_id == request.participant_list_id)
        .all()
    )

    grouped_entries = {}
    for entry in entries:
        key = _normalize_company_name(entry.raw_team_name) or _normalize_text(entry.raw_team_name) or str(entry.id)
        grouped_entries.setdefault(key, []).append(entry)

    matched_entries = 0

    for group in grouped_entries.values():
        representative = group[0]
        company_name = (representative.raw_team_name or representative.raw_full_name or "").strip()
        participant_names = [entry.raw_full_name for entry in group if entry.raw_full_name]

        explicit_uei = next((value for value in (_extract_marker(entry.notes, "UEI") for entry in group) if value), None)
        explicit_duns = next((value for value in (_extract_marker(entry.notes, "DUNS") for entry in group) if value), None)

        website_domain = None
        for entry in group:
            website_domain = _extract_domain(_extract_marker(entry.notes, "Website"))
            if website_domain:
                break
            inferred_domain = _extract_domain(entry.raw_email)
            if inferred_domain and inferred_domain not in FREE_EMAIL_DOMAINS:
                website_domain = inferred_domain
                break

        candidates = []
        seen_candidate_keys = set()

        if explicit_uei:
            for candidate in db.query(SamEntityPublicV2).filter(SamEntityPublicV2.uei == explicit_uei).all():
                candidate_key = candidate.uei or str(candidate.id)
                if candidate_key not in seen_candidate_keys:
                    candidates.append(candidate)
                    seen_candidate_keys.add(candidate_key)

        if explicit_duns:
            for candidate in db.query(SamEntityPublicV2).filter(SamEntityPublicV2.cage_code == explicit_duns).limit(10).all():
                candidate_key = candidate.uei or str(candidate.id)
                if candidate_key not in seen_candidate_keys:
                    candidates.append(candidate)
                    seen_candidate_keys.add(candidate_key)

        normalized_company = _normalize_company_name(company_name)
        if normalized_company:
            tokens = normalized_company.split()
            search_terms = []
            if normalized_company:
                search_terms.append(normalized_company)
            if tokens:
                search_terms.append(tokens[0])
            if len(tokens) >= 2:
                search_terms.append(" ".join(tokens[:2]))

            for search_term in dict.fromkeys(term for term in search_terms if term):
                candidate_rows = (
                    db.query(SamEntityPublicV2)
                    .filter(
                        or_(
                            SamEntityPublicV2.legal_business_name.ilike(f"%{search_term}%"),
                            SamEntityPublicV2.dba_name.ilike(f"%{search_term}%"),
                        )
                    )
                    .limit(40)
                    .all()
                )
                for candidate in candidate_rows:
                    candidate_key = candidate.uei or str(candidate.id)
                    if candidate_key not in seen_candidate_keys:
                        candidates.append(candidate)
                        seen_candidate_keys.add(candidate_key)

        best_entity = None
        best_score = 0.0
        best_match_type = "company"
        best_rationale = None

        for candidate in candidates:
            exact_uei_match = bool(explicit_uei and candidate.uei == explicit_uei)
            score, rationale = _score_candidate(
                company_name=company_name,
                website_domain=website_domain,
                participant_names=participant_names,
                sam_entity=candidate,
                exact_uei_match=exact_uei_match,
            )
            candidate_match_type = "uei" if exact_uei_match else "company"

            if best_entity is None:
                should_replace = True
            elif candidate_match_type == "uei" and best_match_type != "uei":
                should_replace = True
            elif candidate_match_type == best_match_type and score > best_score:
                should_replace = True
            else:
                should_replace = False

            if should_replace:
                best_entity = candidate
                best_score = score
                best_match_type = candidate_match_type
                best_rationale = rationale

        if not best_entity or _match_level(best_score) == "low":
            continue

        company = _get_or_create_company(db, best_entity)

        for entry in group:
            active_matches = (
                db.query(ParticipantCompanyMatch)
                .filter(
                    ParticipantCompanyMatch.participant_list_entry_id == entry.id,
                    ParticipantCompanyMatch.is_active.is_(True),
                )
                .all()
            )

            preferred_match = None
            for existing_match in active_matches:
                if existing_match.company_id == company.id and existing_match.match_type == best_match_type:
                    preferred_match = existing_match
                else:
                    existing_match.is_active = False

            rationale_payload = json.dumps(best_rationale or {})
            if preferred_match:
                preferred_match.confidence = best_score
                preferred_match.match_source = "sam_weighted"
                preferred_match.rationale = rationale_payload
            else:
                match = ParticipantCompanyMatch(
                    participant_list_entry_id=entry.id,
                    company_id=company.id,
                    match_type=best_match_type,
                    match_source="sam_weighted",
                    confidence=best_score,
                    rationale=rationale_payload,
                )
                db.add(match)

            entry.notes = _upsert_marker(entry.notes, "UEI", company.uei)
            entry.notes = _upsert_marker(entry.notes, "Website", best_entity.website)
            matched_entries += 1

    participant_list.status = "ready_for_enrichment" if matched_entries else "uploaded"
    job.status = "completed"
    job.finished_at = datetime.utcnow()
    db.commit()

    return MatchingResponse(
        job_id=job.id,
        status=job.status,
        created_at=job.created_at,
        participant_list_id=job.participant_list_id,
        job_type=job.job_type,
    )


@router.get("/jobs/{job_id}", response_model=MatchJobStatusResponse)
def get_job_status(job_id: UUID, db: Session = Depends(get_db)):
    job = db.query(MatchJob).filter(MatchJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Match job not found")
    return MatchJobStatusResponse(
        job_id=job.id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.get("/results", response_model=MatchResultsResponse)
def get_match_results(participant_list_id: UUID, db: Session = Depends(get_db)):
    matches = (
        db.query(ParticipantCompanyMatch)
        .filter(
            ParticipantCompanyMatch.is_active.is_(True),
            ParticipantCompanyMatch.participant_list_entry_id.in_(
                db.query(ParticipantListEntry.id).filter(
                    ParticipantListEntry.participant_list_id == participant_list_id
                )
            ),
        )
        .all()
    )

    results = []
    for match in matches:
        confidence = float(match.confidence or 0)
        level = _match_level(confidence)
        if level == "low":
            continue

        entry = db.query(ParticipantListEntry).filter(ParticipantListEntry.id == match.participant_list_entry_id).first()
        company = db.query(Company).filter(Company.id == match.company_id).first()
        sam_profile = None
        if company and company.uei:
            sam_entity = db.query(SamEntityPublicV2).filter(SamEntityPublicV2.uei == company.uei).first()
            if sam_entity:
                sam_profile = {
                    "uei": sam_entity.uei,
                    "legal_business_name": sam_entity.legal_business_name,
                    "cage_code": sam_entity.cage_code,
                    "registration_status": sam_entity.registration_status,
                    "address": {
                        "line1": sam_entity.address_line1,
                        "city": sam_entity.city,
                        "state": sam_entity.state,
                        "zip": sam_entity.zip_code,
                        "country": sam_entity.country,
                    },
                }

        rationale = None
        if match.rationale:
            try:
                rationale = json.loads(match.rationale)
            except json.JSONDecodeError:
                rationale = {"summary": match.rationale}

        results.append(
            MatchResultResponse(
                match_id=match.id,
                participant=MatchParticipantResponse(
                    id=entry.id,
                    team_name=entry.raw_team_name,
                    full_name=entry.raw_full_name,
                    email=entry.raw_email,
                ),
                company=MatchCompanyResponse(
                    id=company.id,
                    name=company.name,
                    uei=company.uei,
                    duns=company.duns,
                    city=company.city,
                    state=company.state,
                )
                if company
                else None,
                confidence=confidence,
                match_level=level,
                match_type=match.match_type,
                match_source=match.match_source,
                sam_profile=sam_profile,
                rationale=rationale,
            )
        )

    return MatchResultsResponse(results=results, total=len(results))


@router.delete("/results/{match_id}", response_model=RemoveMatchResponse)
def remove_match(match_id: UUID, db: Session = Depends(get_db)):
    match = db.query(ParticipantCompanyMatch).filter(ParticipantCompanyMatch.id == match_id).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    match.is_active = False
    db.commit()
    return RemoveMatchResponse(match_id=match.id, removed=True)
