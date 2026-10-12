"""
WATN Data Ingestion Service
----------------------------
Handles parsing, cleaning, and idempotent incremental ingestion of Salesforce exports:
1. Cohorts      (Events & Cohorts)
2. Companies    (Companies & Aliases)
3. Participants (Participants, ParticipantListEntries, Teams, TeamMembers, CompanyMembers, Matches)
4. Funding      (FundingRecords linked to Companies)

Design Principles:
- Idempotent: Can be run repeatedly; only adds new content and enriches existing empty fields.
- Robust Parser: Automatically detects Salesforce report header row and strips footers.
- Unified: Works with file paths, file-like objects, or FastAPI UploadFile objects.
- Relational Integrity: Automatically links entities, creates stubs for missing references,
  and handles constraints gracefully.
"""

import io
import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pandas as pd
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.db.base import SessionLocal
from app.models.cohorts import Cohort
from app.models.company import Company, CompanyAlias, ParticipantCompanyMatch
from app.models.company_members import CompanyMembers
from app.models.events import Event, EventUniversity
from app.models.funding import FundingRecord
from app.models.hub import Hub
from app.models.participant_lists import ParticipantList
from app.models.participants import ParticipantListEntry, Participants
from app.models.team_members import TeamMembers
from app.models.teams import Teams
from app.models.university import University
from app.models.users import User

logger = logging.getLogger(__name__)

# Canonical institution mappings
INSTITUTION_ALIASES: Dict[str, str] = {
    "umd": "The University of Maryland, College Park",
    "university of maryland": "The University of Maryland, College Park",
    "university of maryland - college park": "The University of Maryland, College Park",
    "university of maryland college park": "The University of Maryland, College Park",
    "vt": "Virginia Polytechnic Institute and State University",
    "virginia tech": "Virginia Polytechnic Institute and State University",
    "virginia polytechnic institute and state university": "Virginia Polytechnic Institute and State University",
    "gwu": "The George Washington University",
    "gw": "The George Washington University",
    "george washington university": "The George Washington University",
    "the george washington university": "The George Washington University",
    "jhu": "Johns Hopkins University",
    "johns hopkins": "Johns Hopkins University",
    "johns hopkins university": "Johns Hopkins University",
    "psu": "The Pennsylvania State University",
    "penn state": "The Pennsylvania State University",
    "penn state university": "The Pennsylvania State University",
    "the pennsylvania state university": "The Pennsylvania State University",
    "upenn": "University of Pennsylvania",
    "university of pennsylvania": "University of Pennsylvania",
    "cmu": "Carnegie Mellon University",
    "carnegie mellon": "Carnegie Mellon University",
    "carnegie mellon university": "Carnegie Mellon University",
    "ncsu": "North Carolina State University",
    "nc state": "North Carolina State University",
    "north carolina state university": "North Carolina State University",
    "uncch": "The University of North Carolina at Chapel Hill",
    "unc": "The University of North Carolina at Chapel Hill",
    "unc chapel hill": "The University of North Carolina at Chapel Hill",
    "the university of north carolina at chapel hill": "The University of North Carolina at Chapel Hill",
    "hamp": "Hampton University",
    "hampton": "Hampton University",
    "hampton university": "Hampton University",
    "hu": "Howard University",
    "howard": "Howard University",
    "howard university": "Howard University",
    "uva": "The University of Virginia",
    "university of virginia": "The University of Virginia",
    "gmu": "George Mason University",
    "george mason university": "George Mason University",
    "vcu": "Virginia Commonwealth University",
    "virginia commonwealth university": "Virginia Commonwealth University",
    "msu": "Morgan State University",
    "morgan state": "Morgan State University",
    "morgan state university": "Morgan State University",
    "umb": "University of Maryland, Baltimore",
    "university of maryland, baltimore": "University of Maryland, Baltimore",
}

VALID_FUNDING_SOURCES = {
    "Angel",
    "Accelerator or Incubator",
    "Competition or Prize",
    "Cooperative Agreement",
    "Definitive Contract",
    "Delivery Order",
    "Equity Crowdfunding",
    "Federal",
    "Grant",
    "MIPS",
    "NIH",
    "NSF I-Corps",
    "Other",
    "Purchase Order",
    "SBIR",
    "Series",
    "STTR",
    "TEDCO",
    "Venture Capital",
}

VALID_FUNDING_STAGES = {
    "Stage 1: Pre-Seed Funding",
    "Stage 2: Seed Funding",
    "Stage 3: Early Stage Investment (Series A & B)",
    "Stage 4: Later Stage Investment (Series C, D, etc.)",
    "Stage 5: Mezzanine Financing",
    "Stage 6: Revenue & Commercial Traction",
}

MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


# ---------------------------------------------------------------------------
# Value Cleaning Helpers
# ---------------------------------------------------------------------------


def _clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text_val = str(value).strip()
    return re.sub(r"\s+", " ", text_val)


def _clean_nullable(value: Any) -> Optional[str]:
    cleaned = _clean_text(value)
    if not cleaned or cleaned.lower() in {"-", "--", "n/a", "na", "none", "null", "nan"}:
        return None
    return cleaned


def _normalize_identifier(value: Any) -> Optional[str]:
    cleaned = _clean_nullable(value)
    if not cleaned:
        return None
    cleaned = cleaned.replace(" ", "").upper()
    return cleaned or None


def _normalize_company_name(name: str) -> str:
    cleaned = _clean_text(name).lower()
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_domain(url: Optional[str]) -> Optional[str]:
    cleaned = _clean_nullable(url)
    if not cleaned:
        return None
    if not re.match(r"^[a-zA-Z]+://", cleaned):
        cleaned = "http://" + cleaned
    try:
        parsed = urlparse(cleaned)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain or None
    except Exception:
        return None


def _parse_date(value: Any) -> Optional[date]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()

    cleaned = _clean_nullable(str(value))
    if not cleaned:
        return None

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_month(value: Any) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not pd.isna(value):
        iv = int(value)
        return iv if 1 <= iv <= 12 else None

    cleaned = _clean_nullable(str(value))
    if not cleaned:
        return None

    return MONTH_MAP.get(cleaned.lower())


def _parse_amount(value: Any) -> Optional[Decimal]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    cleaned = _clean_nullable(str(value))
    if not cleaned:
        return None
    cleaned = re.sub(r"[^\d.-]", "", cleaned)
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def _parse_bool(value: Any) -> Optional[bool]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    cleaned = _clean_text(str(value)).lower()
    if cleaned in ("true", "1", "yes", "y"):
        return True
    if cleaned in ("false", "0", "no", "n"):
        return False
    return None


# ---------------------------------------------------------------------------
# Salesforce Report Parsing
# ---------------------------------------------------------------------------


def parse_salesforce_file(
    file_source: Union[str, BinaryIO, bytes],
    filename: Optional[str] = None,
) -> pd.DataFrame:
    """
    Parse an Excel (.xlsx, .xls) or CSV Salesforce export report.
    Automatically identifies the header row by matching signature Salesforce column names,
    drops header banners and footer rows (Totals, Count, Confidentiality statements).
    """
    if isinstance(file_source, bytes):
        file_source = io.BytesIO(file_source)

    fname = (filename or (file_source if isinstance(file_source, str) else "")).lower()

    if fname.endswith(".csv"):
        raw = pd.read_csv(file_source, header=None, dtype=str)
    else:
        # Default to Excel
        raw = pd.read_excel(file_source, header=None)

    # Locate the header row by searching for signature column headers
    signature_headers = {
        "cohort class: cohortid",
        "i-corps team: corpid",
        "entrepreneur: eid",
        "funding: funding id",
        "account name",
    }

    header_idx = -1
    for idx, row in raw.iterrows():
        non_null = [str(v).strip().lower() for v in row if pd.notna(v) and str(v).strip()]
        if any(sig in non_null for sig in signature_headers):
            header_idx = idx
            break

    if header_idx == -1:
        # Fallback to row 0 if no signature header found
        header_idx = 0

    headers = [
        str(c).strip() if pd.notna(c) and str(c).strip() else f"_unnamed_{i}"
        for i, c in enumerate(raw.iloc[header_idx])
    ]

    df = raw.iloc[header_idx + 1 :].copy()
    df.columns = headers

    # Drop unnamed columns that are entirely NaN
    unnamed_cols = [c for c in df.columns if c.startswith("_unnamed_")]
    for c in unnamed_cols:
        if df[c].isna().all():
            df = df.drop(columns=[c])

    # Filter out footer rows
    # Footers typically have 'Total', 'Count', 'Confidential', or 'Copyright'
    first_col = df.columns[0]
    df = df[df[first_col].notna()]
    mask = ~df[first_col].astype(str).str.strip().str.startswith(
        ("Total", "Count", "Confidential Information", "Copyright ©", "Copyright (c)")
    )
    df = df[mask]

    # Also drop rows where all columns are empty
    df = df.dropna(how="all")

    return df


# ---------------------------------------------------------------------------
# University & Hub Helpers
# ---------------------------------------------------------------------------


def get_default_hub(db: Session) -> Hub:
    hub = db.query(Hub).filter(Hub.name == "Mid-Atlantic Hub").first()
    if not hub:
        hub = db.query(Hub).first()
    if not hub:
        hub = Hub(name="Mid-Atlantic Hub")
        db.add(hub)
        db.flush()
    return hub


def get_system_user(db: Session) -> User:
    user = db.query(User).filter(User.email == "system@watn.edu").first()
    if not user:
        user = User(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            full_name="WATN System Admin",
            email="system@watn.edu",
            password_hash="",
            role="admin",
            is_active=True,
        )
        db.add(user)
        db.flush()
    return user


def resolve_lead_university(
    cohort_name: str,
    db: Session,
    hub_id: UUID,
) -> Optional[UUID]:
    """
    Extracts institution abbreviation from cohort name like 'Spring 2021 (Apr) Intro Course (VT)'
    and resolves it to a University record ID. Creates a new University if not found.
    """
    # Look for abbreviation in parentheses at the end or within name
    match = re.search(r"\(([^)]+)\)\s*$", cohort_name)
    if not match:
        return None

    inst_key = match.group(1).strip().lower()
    canonical_name = INSTITUTION_ALIASES.get(inst_key)

    if canonical_name:
        uni = db.query(University).filter(University.name.ilike(canonical_name)).first()
        if uni:
            return uni.id
        # Create university if known canonical name
        new_uni = University(name=canonical_name, hub_id=hub_id, country="United States")
        db.add(new_uni)
        db.flush()
        return new_uni.id

    # Try matching directly by university name
    uni = db.query(University).filter(
        or_(
            University.name.ilike(inst_key),
            University.name.ilike(f"%{inst_key}%"),
        )
    ).first()
    if uni:
        return uni.id

    return None


# ---------------------------------------------------------------------------
# 1. Cohorts Ingestion
# ---------------------------------------------------------------------------


def ingest_cohorts_dataframe(
    df: pd.DataFrame,
    db: Session,
    hub_id: Optional[UUID] = None,
    filename: str = "Cohorts.xlsx",
) -> Dict[str, Any]:
    """
    Ingests Cohort records from parsed DataFrame.
    Updates Events and Cohorts tables idempotently.
    """
    if hub_id is None:
        hub = get_default_hub(db)
        hub_id = hub.id

    system_user = get_system_user(db)

    stats = {
        "total_records": len(df),
        "cohorts_created": 0,
        "cohorts_updated": 0,
        "events_created": 0,
        "events_updated": 0,
        "errors": [],
    }

    # Find relevant columns
    col_cid = next((c for c in df.columns if "cohortid" in c.lower() or "cohort id" in c.lower()), None)
    col_name = next((c for c in df.columns if c.strip().lower() == "cohort" or "cohort name" in c.lower()), None)
    col_date = next((c for c in df.columns if "start date" in c.lower() or "start_date" in c.lower()), None)

    if not col_cid and not col_name:
        stats["errors"].append("Cohorts file missing required columns (Cohort Class: CohortID or Cohort).")
        return stats

    for _, row in df.iterrows():
        try:
            cohort_id = _normalize_identifier(row.get(col_cid)) if col_cid else None
            name = _clean_text(row.get(col_name)) if col_name else ""
            start_date = _parse_date(row.get(col_date)) if col_date else None

            if not name and not cohort_id:
                continue

            lead_uni_id = resolve_lead_university(name, db, hub_id)
            is_seven_week = "7-week" in name.lower() or "seven week" in name.lower()

            # --- 1. Upsert Event ---
            event: Optional[Event] = None
            if cohort_id:
                event = db.query(Event).filter(Event.cohort_id == cohort_id).first()
            if not event and name:
                event = db.query(Event).filter(
                    Event.hub_organization_id == hub_id,
                    Event.name.ilike(name),
                ).first()

            if event:
                changed = False
                if cohort_id and not event.cohort_id:
                    event.cohort_id = cohort_id
                    changed = True
                if start_date and not event.start_date:
                    event.start_date = start_date
                    changed = True
                if lead_uni_id and not event.lead_university_id:
                    event.lead_university_id = lead_uni_id
                    changed = True
                if changed:
                    stats["events_updated"] += 1
            else:
                event = Event(
                    hub_organization_id=hub_id,
                    cohort_id=cohort_id,
                    name=name,
                    event_type="I-Corps Cohort",
                    start_date=start_date,
                    lead_university_id=lead_uni_id,
                    is_seven_week_program=is_seven_week,
                    created_by_user_id=system_user.id,
                )
                db.add(event)
                db.flush()
                stats["events_created"] += 1

                if lead_uni_id:
                    db.add(
                        EventUniversity(
                            event_id=event.id,
                            university_id=lead_uni_id,
                            involvement_role="lead",
                        )
                    )

            # Ensure ParticipantList exists for this event
            plist = db.query(ParticipantList).filter(ParticipantList.event_id == event.id).first()
            if not plist:
                plist = ParticipantList(
                    event_id=event.id,
                    published_by_user_id=system_user.id,
                    university_id=lead_uni_id,
                    title=f"Salesforce Import - {name or cohort_id}",
                    source_type="salesforce_excel",
                    source_filename=filename,
                    status="uploaded",
                )
                db.add(plist)
                db.flush()

            # --- 2. Upsert Cohort ---
            cohort: Optional[Cohort] = None
            if cohort_id:
                cohort = db.query(Cohort).filter(Cohort.cohort_id == cohort_id).first()
            if not cohort and name:
                cohort = db.query(Cohort).filter(
                    Cohort.hub_id == hub_id,
                    Cohort.cohort_name.ilike(name),
                ).first()

            if cohort:
                changed = False
                if cohort_id and not cohort.cohort_id:
                    cohort.cohort_id = cohort_id
                    changed = True
                if start_date and not cohort.start_date:
                    cohort.start_date = start_date
                    changed = True
                if changed:
                    stats["cohorts_updated"] += 1
            else:
                cohort_type_str = "7 week program" if is_seven_week else "Short Course"
                cohort = Cohort(
                    hub_id=hub_id,
                    cohort_id=cohort_id,
                    cohort_name=name,
                    start_date=start_date,
                    cohort_type=cohort_type_str,
                )
                db.add(cohort)
                db.flush()
                stats["cohorts_created"] += 1

        except Exception as e:
            stats["errors"].append(f"Cohort row error: {str(e)}")

    db.commit()
    return stats


# ---------------------------------------------------------------------------
# 2. Companies Ingestion
# ---------------------------------------------------------------------------


def ingest_companies_dataframe(
    df: pd.DataFrame,
    db: Session,
) -> Dict[str, Any]:
    """
    Ingests Company records from parsed DataFrame.
    Upserts companies idempotently based on normalized name, UEI, or DUNS.
    """
    stats = {
        "total_records": len(df),
        "companies_created": 0,
        "companies_updated": 0,
        "errors": [],
    }

    col_name = next((c for c in df.columns if "account name" in c.lower() or "company" in c.lower()), None)
    col_uei = next((c for c in df.columns if "uei" in c.lower()), None)
    col_duns = next((c for c in df.columns if "duns" in c.lower()), None)
    col_web = next((c for c in df.columns if "website" in c.lower() or "url" in c.lower()), None)

    if not col_name:
        stats["errors"].append("Companies file missing required 'Account Name' column.")
        return stats

    # Cache existing companies by normalized_name, uei, and duns
    existing_by_norm: Dict[str, Company] = {}
    existing_by_uei: Dict[str, Company] = {}
    existing_by_duns: Dict[str, Company] = {}

    for c in db.query(Company).all():
        if c.normalized_name:
            existing_by_norm[c.normalized_name] = c
        if c.uei:
            existing_by_uei[c.uei.upper()] = c
        if c.duns:
            existing_by_duns[c.duns.strip()] = c

    for _, row in df.iterrows():
        try:
            name = _clean_text(row.get(col_name))
            if not name:
                continue

            norm_name = _normalize_company_name(name)
            uei = _normalize_identifier(row.get(col_uei)) if col_uei else None
            duns = _clean_nullable(str(row.get(col_duns))) if col_duns else None
            if duns and duns.endswith(".0"):
                duns = duns[:-2]
            website = _clean_nullable(row.get(col_web)) if col_web else None
            domain = _extract_domain(website)

            # Match against existing company
            company = existing_by_norm.get(norm_name)
            if not company and uei:
                company = existing_by_uei.get(uei)
            if not company and duns:
                company = existing_by_duns.get(duns)

            if company:
                # Merge / enrich existing fields if currently empty
                changed = False
                if uei and not company.uei:
                    company.uei = uei
                    existing_by_uei[uei] = company
                    changed = True
                if duns and not company.duns:
                    company.duns = duns
                    existing_by_duns[duns] = company
                    changed = True
                if website and not company.website_url:
                    company.website_url = website
                    changed = True
                if domain and not company.domain:
                    company.domain = domain
                    changed = True
                if changed:
                    stats["companies_updated"] += 1
            else:
                # Create new company
                company = Company(
                    name=name,
                    normalized_name=norm_name,
                    uei=uei,
                    duns=duns,
                    website_url=website,
                    domain=domain,
                )
                db.add(company)
                db.flush()

                if norm_name:
                    existing_by_norm[norm_name] = company
                if uei:
                    existing_by_uei[uei] = company
                if duns:
                    existing_by_duns[duns] = company

                stats["companies_created"] += 1

        except Exception as e:
            stats["errors"].append(f"Company row error ({name}): {str(e)}")

    db.commit()
    return stats


# ---------------------------------------------------------------------------
# 3. Participants Ingestion
# ---------------------------------------------------------------------------


def ingest_participants_dataframe(
    df: pd.DataFrame,
    db: Session,
    hub_id: Optional[UUID] = None,
    filename: str = "Participants.xlsx",
) -> Dict[str, Any]:
    """
    Ingests Participants, ParticipantListEntries, Teams, TeamMembers, and CompanyMembers.
    Idempotently handles existing records.
    """
    if hub_id is None:
        hub = get_default_hub(db)
        hub_id = hub.id

    system_user = get_system_user(db)

    stats = {
        "total_records": len(df),
        "participants_created": 0,
        "participants_updated": 0,
        "entries_created": 0,
        "teams_created": 0,
        "matches_created": 0,
        "company_members_created": 0,
        "errors": [],
    }

    # Column identification
    col_eid = next((c for c in df.columns if "entrepreneur: eid" in c.lower() or c.lower() == "eid"), None)
    col_team_id = next((c for c in df.columns if "corpid" in c.lower() or "team id" in c.lower()), None)
    col_team_name = next((c for c in df.columns if "team name" in c.lower()), None)
    col_first = next((c for c in df.columns if "first name" in c.lower()), None)
    col_last = next((c for c in df.columns if "last name" in c.lower()), None)
    col_email = next((c for c in df.columns if c.strip().lower() == "email"), None)
    col_alt_email = next((c for c in df.columns if "alternate email" in c.lower()), None)
    col_linkedin = next((c for c in df.columns if "linkedin" in c.lower()), None)
    col_affil = next((c for c in df.columns if "university affiliation" in c.lower()), None)
    col_other_affil = next((c for c in df.columns if "other university affiliation" in c.lower()), None)
    col_company = next((c for c in df.columns if "company name" in c.lower() or c.lower() == "company"), None)
    col_cohort_id = next((c for c in df.columns if "regional cohort" in c.lower() or "cohortid" in c.lower()), None)

    # Cache participants by eid and email
    participants_by_eid: Dict[str, Participants] = {}
    participants_by_email: Dict[str, Participants] = {}
    for p in db.query(Participants).all():
        if p.eid:
            participants_by_eid[p.eid.upper()] = p
        if p.primary_email:
            participants_by_email[p.primary_email.lower()] = p

    # Cache companies by normalized name
    companies_by_norm: Dict[str, Company] = {
        c.normalized_name: c for c in db.query(Company).filter(Company.normalized_name.isnot(None)).all()
    }

    # Cache events by cohort_id
    events_by_cid: Dict[str, Event] = {
        e.cohort_id.upper(): e for e in db.query(Event).filter(Event.cohort_id.isnot(None)).all()
    }

    # Cache cohorts by cohort_id
    cohorts_by_cid: Dict[str, Cohort] = {
        c.cohort_id.upper(): c for c in db.query(Cohort).filter(Cohort.cohort_id.isnot(None)).all()
    }

    # Cache participant lists by event_id
    plists_by_event: Dict[UUID, ParticipantList] = {
        pl.event_id: pl for pl in db.query(ParticipantList).all()
    }

    # Track existing entries to prevent duplication: (participant_list_id, participant_id)
    existing_entries: set = {
        (e.participant_list_id, e.participant_id)
        for e in db.query(ParticipantListEntry.participant_list_id, ParticipantListEntry.participant_id).all()
        if e.participant_id is not None
    }

    # Track next row number per participant list
    max_row_query = (
        db.query(ParticipantListEntry.participant_list_id, func.max(ParticipantListEntry.row_number))
        .group_by(ParticipantListEntry.participant_list_id)
        .all()
    )
    next_row_num: Dict[UUID, int] = {
        pl_id: (max_r or 0) + 1 for pl_id, max_r in max_row_query
    }

    # Track existing matches: (participant_list_entry_id, company_id)
    existing_matches: set = {
        (m.participant_list_entry_id, m.company_id)
        for m in db.query(ParticipantCompanyMatch.participant_list_entry_id, ParticipantCompanyMatch.company_id).all()
    }

    # Track existing company members: (company_id, participant_id)
    existing_members: set = {
        (cm.company_id, cm.participant_id)
        for cm in db.query(CompanyMembers.company_id, CompanyMembers.participant_id).all()
    }

    # Track existing teams: (cohort_id, team_name.lower())
    existing_teams: Dict[Tuple[UUID, str], Teams] = {
        (t.cohort_id, t.team_name.lower().strip()): t
        for t in db.query(Teams).all()
    }

    # Track existing team members: (team_id, participant_id)
    existing_team_members: set = {
        (tm.team_id, tm.participant_id)
        for tm in db.query(TeamMembers.team_id, TeamMembers.participant_id).all()
    }

    for _, row in df.iterrows():
        try:
            eid = _normalize_identifier(row.get(col_eid)) if col_eid else None
            first_name = _clean_text(row.get(col_first)) if col_first else ""
            last_name = _clean_text(row.get(col_last)) if col_last else ""
            email = _clean_nullable(row.get(col_email)) if col_email else None
            email_lower = email.lower() if email else None
            linkedin = _clean_nullable(row.get(col_linkedin)) if col_linkedin else None
            team_name = _clean_nullable(row.get(col_team_name)) if col_team_name else None
            company_name = _clean_nullable(row.get(col_company)) if col_company else None
            cohort_id_raw = _normalize_identifier(row.get(col_cohort_id)) if col_cohort_id else None

            affil = _clean_nullable(row.get(col_affil)) if col_affil else None
            other_affil = _clean_nullable(row.get(col_other_affil)) if col_other_affil else None
            combined_affil = affil or other_affil

            if not first_name and not last_name and not email and not eid:
                continue

            # --- 1. Upsert Participant ---
            participant: Optional[Participants] = None
            if eid and eid in participants_by_eid:
                participant = participants_by_eid[eid]
            elif email_lower and email_lower in participants_by_email:
                participant = participants_by_email[email_lower]

            if participant:
                changed = False
                if eid and not participant.eid:
                    participant.eid = eid
                    participants_by_eid[eid] = participant
                    changed = True
                if email_lower and not participant.primary_email:
                    participant.primary_email = email_lower
                    participants_by_email[email_lower] = participant
                    changed = True
                if linkedin and not participant.linkedin_url:
                    participant.linkedin_url = linkedin
                    changed = True
                if first_name and participant.first_name in ("", "Unknown"):
                    participant.first_name = first_name
                    changed = True
                if last_name and participant.last_name in ("", "Unknown"):
                    participant.last_name = last_name
                    changed = True
                if changed:
                    stats["participants_updated"] += 1
            else:
                participant = Participants(
                    eid=eid,
                    primary_email=email_lower,
                    first_name=first_name or "Unknown",
                    last_name=last_name or "Unknown",
                    linkedin_url=linkedin,
                )
                db.add(participant)
                db.flush()
                if eid:
                    participants_by_eid[eid] = participant
                if email_lower:
                    participants_by_email[email_lower] = participant
                stats["participants_created"] += 1

            # --- 2. Resolve Cohort / Event & ParticipantList ---
            event: Optional[Event] = None
            cohort: Optional[Cohort] = None
            if cohort_id_raw:
                event = events_by_cid.get(cohort_id_raw)
                cohort = cohorts_by_cid.get(cohort_id_raw)

            if not event and cohort_id_raw:
                # Stub event if cohort not previously imported
                event = Event(
                    hub_organization_id=hub_id,
                    cohort_id=cohort_id_raw,
                    name=f"Cohort {cohort_id_raw}",
                    event_type="I-Corps Cohort",
                    created_by_user_id=system_user.id,
                )
                db.add(event)
                db.flush()
                events_by_cid[cohort_id_raw] = event

            if not cohort and cohort_id_raw:
                cohort = Cohort(
                    hub_id=hub_id,
                    cohort_id=cohort_id_raw,
                    cohort_name=f"Cohort {cohort_id_raw}",
                )
                db.add(cohort)
                db.flush()
                cohorts_by_cid[cohort_id_raw] = cohort

            entry_id: Optional[UUID] = None
            if event:
                plist = plists_by_event.get(event.id)
                if not plist:
                    plist = ParticipantList(
                        event_id=event.id,
                        published_by_user_id=system_user.id,
                        title=f"Salesforce Import - {event.name or cohort_id_raw}",
                        source_type="salesforce_excel",
                        source_filename=filename,
                        status="uploaded",
                    )
                    db.add(plist)
                    db.flush()
                    plists_by_event[event.id] = plist

                # --- 3. Upsert ParticipantListEntry ---
                entry_key = (plist.id, participant.id)
                if entry_key not in existing_entries:
                    row_num = next_row_num.get(plist.id, 1)
                    next_row_num[plist.id] = row_num + 1

                    full_name = f"{first_name} {last_name}".strip()
                    entry = ParticipantListEntry(
                        participant_list_id=plist.id,
                        participant_id=participant.id,
                        row_number=row_num,
                        raw_full_name=full_name or None,
                        raw_email=email_lower,
                        raw_affiliation=combined_affil,
                        raw_team_name=team_name,
                        raw_company_name=company_name,
                    )
                    db.add(entry)
                    db.flush()
                    existing_entries.add(entry_key)
                    entry_id = entry.id
                    stats["entries_created"] += 1

            # --- 4. Resolve Company & Link Matches ---
            target_company: Optional[Company] = None
            if company_name:
                norm_cname = _normalize_company_name(company_name)
                target_company = companies_by_norm.get(norm_cname)
                if not target_company:
                    # Create stub company so relationships are preserved
                    target_company = Company(
                        name=company_name,
                        normalized_name=norm_cname,
                    )
                    db.add(target_company)
                    db.flush()
                    companies_by_norm[norm_cname] = target_company

                # Link ParticipantCompanyMatch if entry was created
                if entry_id and (entry_id, target_company.id) not in existing_matches:
                    match = ParticipantCompanyMatch(
                        participant_list_entry_id=entry_id,
                        company_id=target_company.id,
                        match_type="salesforce_import",
                        match_source="input_file",
                        confidence=Decimal("1.0"),
                        is_active=True,
                    )
                    db.add(match)
                    existing_matches.add((entry_id, target_company.id))
                    stats["matches_created"] += 1

                # Link CompanyMembers
                if (target_company.id, participant.id) not in existing_members:
                    db.add(
                        CompanyMembers(
                            company_id=target_company.id,
                            participant_id=participant.id,
                            formal_title="Team Member",
                        )
                    )
                    existing_members.add((target_company.id, participant.id))
                    stats["company_members_created"] += 1

            # --- 5. Resolve Teams & TeamMembers ---
            if cohort and team_name:
                team_key = (cohort.id, team_name.lower().strip())
                team = existing_teams.get(team_key)
                if not team:
                    team = Teams(
                        team_name=team_name,
                        cohort_id=cohort.id,
                    )
                    db.add(team)
                    db.flush()
                    existing_teams[team_key] = team
                    stats["teams_created"] += 1

                if (team.id, participant.id) not in existing_team_members:
                    db.add(
                        TeamMembers(
                            team_id=team.id,
                            participant_id=participant.id,
                            team_role="Participant",
                        )
                    )
                    existing_team_members.add((team.id, participant.id))

        except Exception as e:
            stats["errors"].append(f"Participant row error ({first_name} {last_name}): {str(e)}")

    db.commit()
    return stats


# ---------------------------------------------------------------------------
# 4. Funding Ingestion
# ---------------------------------------------------------------------------


def ingest_funding_dataframe(
    df: pd.DataFrame,
    db: Session,
) -> Dict[str, Any]:
    """
    Ingests Funding records from parsed DataFrame.
    Idempotent: matches by funding_id; skips already ingested records.
    """
    stats = {
        "total_records": len(df),
        "funding_created": 0,
        "funding_skipped_existing": 0,
        "errors": [],
    }

    col_fid = next((c for c in df.columns if "funding: funding id" in c.lower() or "funding id" in c.lower()), None)
    col_acc = next((c for c in df.columns if "account name" in c.lower() or "company" in c.lower()), None)
    col_date = next((c for c in df.columns if "date awarded" in c.lower() or "date" in c.lower()), None)
    col_amt = next((c for c in df.columns if "amount awarded" in c.lower() or "amount" in c.lower()), None)
    col_end_m = next((c for c in df.columns if "end month" in c.lower()), None)
    col_end_y = next((c for c in df.columns if "end year" in c.lower()), None)
    col_source = next((c for c in df.columns if "funding source" in c.lower()), None)
    col_stage = next((c for c in df.columns if "funding stage" in c.lower()), None)
    col_investors = next((c for c in df.columns if "investors" in c.lower()), None)
    col_phase = next((c for c in df.columns if c.strip().lower() == "phase"), None)
    col_post = next((c for c in df.columns if "post i-corps" in c.lower()), None)
    col_url = next((c for c in df.columns if "website" in c.lower() or "url" in c.lower()), None)

    if not col_fid or not col_acc:
        stats["errors"].append("Funding file missing required 'Funding ID' or 'Account Name' column.")
        return stats

    # Cache existing funding IDs
    existing_fids = {
        fid.upper() for (fid,) in db.query(FundingRecord.funding_id).filter(FundingRecord.funding_id.isnot(None)).all()
    }

    # Cache companies by normalized name
    companies_by_norm: Dict[str, Company] = {
        c.normalized_name: c for c in db.query(Company).filter(Company.normalized_name.isnot(None)).all()
    }

    for _, row in df.iterrows():
        try:
            funding_id = _normalize_identifier(row.get(col_fid))
            account_name = _clean_text(row.get(col_acc))

            if not funding_id or not account_name:
                continue

            if funding_id in existing_fids:
                stats["funding_skipped_existing"] += 1
                continue

            norm_acc = _normalize_company_name(account_name)
            company = companies_by_norm.get(norm_acc)
            if not company:
                # Create stub company if not already created
                company = Company(
                    name=account_name,
                    normalized_name=norm_acc,
                )
                db.add(company)
                db.flush()
                companies_by_norm[norm_acc] = company

            date_awarded = _parse_date(row.get(col_date)) if col_date else None
            amount = _parse_amount(row.get(col_amt)) if col_amt else None
            end_month = _parse_month(row.get(col_end_m)) if col_end_m else None
            end_year = None
            if col_end_y and pd.notna(row.get(col_end_y)):
                try:
                    end_year = int(float(row.get(col_end_y)))
                except (ValueError, TypeError):
                    end_year = None

            # Funding source mapping (handling multi-value strings like 'Angel; Venture Capital')
            raw_source = _clean_nullable(row.get(col_source)) if col_source else None
            funding_source_enum = None
            if raw_source:
                first_part = raw_source.split(";")[0].strip()
                if raw_source in VALID_FUNDING_SOURCES:
                    funding_source_enum = raw_source
                elif first_part in VALID_FUNDING_SOURCES:
                    funding_source_enum = first_part
                else:
                    funding_source_enum = "Other"

            # Funding stage mapping
            raw_stage = _clean_nullable(row.get(col_stage)) if col_stage else None
            funding_stage_enum = raw_stage if raw_stage in VALID_FUNDING_STAGES else None

            investors = _clean_nullable(row.get(col_investors)) if col_investors else None
            phase = _clean_nullable(row.get(col_phase)) if col_phase else None
            post_icorps = _parse_bool(row.get(col_post)) if col_post else None
            source_url = _clean_nullable(row.get(col_url)) if col_url else None

            funding_record = FundingRecord(
                company_id=company.id,
                funding_id=funding_id,
                amount_awarded=amount,
                date_awarded=date_awarded,
                award_end_month=end_month,
                award_end_year=end_year,
                funding_source=funding_source_enum,
                funding_type=raw_source,  # Preserves full raw string
                funding_stage=funding_stage_enum,
                investors=investors,
                phase=phase,
                received_post_icorps=post_icorps,
                source_url=source_url,
                award_link=source_url,
            )
            db.add(funding_record)
            existing_fids.add(funding_id)
            stats["funding_created"] += 1

        except Exception as e:
            stats["errors"].append(f"Funding row error ({funding_id}): {str(e)}")

    db.commit()
    return stats


# ---------------------------------------------------------------------------
# Master Ingestion Orchestrator
# ---------------------------------------------------------------------------


def ingest_all_watn_files(
    db: Session,
    cohorts_source: Optional[Union[str, BinaryIO, bytes]] = None,
    companies_source: Optional[Union[str, BinaryIO, bytes]] = None,
    participants_source: Optional[Union[str, BinaryIO, bytes]] = None,
    funding_source: Optional[Union[str, BinaryIO, bytes]] = None,
    hub_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """
    Ingests all 4 WATN input files in optimal dependency order:
    1. Cohorts
    2. Companies
    3. Participants
    4. Funding
    """
    summary: Dict[str, Any] = {
        "status": "success",
        "cohorts": {},
        "companies": {},
        "participants": {},
        "funding": {},
        "errors": [],
    }

    try:
        # 1. Cohorts
        if cohorts_source:
            logger.info("Ingesting Cohorts...")
            df_cohorts = parse_salesforce_file(cohorts_source, filename="Cohorts.xlsx")
            summary["cohorts"] = ingest_cohorts_dataframe(df_cohorts, db, hub_id=hub_id)

        # 2. Companies
        if companies_source:
            logger.info("Ingesting Companies...")
            df_companies = parse_salesforce_file(companies_source, filename="Companies.xlsx")
            summary["companies"] = ingest_companies_dataframe(df_companies, db)

        # 3. Participants
        if participants_source:
            logger.info("Ingesting Participants...")
            df_participants = parse_salesforce_file(participants_source, filename="Participants.xlsx")
            summary["participants"] = ingest_participants_dataframe(df_participants, db, hub_id=hub_id)

        # 4. Funding
        if funding_source:
            logger.info("Ingesting Funding...")
            df_funding = parse_salesforce_file(funding_source, filename="Funding.xlsx")
            summary["funding"] = ingest_funding_dataframe(df_funding, db)

    except Exception as exc:
        db.rollback()
        logger.exception("Error during WATN data ingestion: %s", exc)
        summary["status"] = "failed"
        summary["errors"].append(str(exc))

    return summary
