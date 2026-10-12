"""
Full data pipeline orchestrator.

Entry points
------------
run_pipeline_for_company(company_id, sources, db) → dict
run_pipeline_batch(job_id, company_ids, sources)   → runs in a background thread

Flow per company
-----------------
1. Fetch existing internal record.
2. Resolve identifiers  (DUNS → UEI, then name-based SAM search).
3. Update company profile from SAM.gov (priority source).
4. Run multi-source enrichment (SBIR, NSF, USASpending, Census, USPTO).
5. Classify every funding record (source + stage).
6. Upsert investor entities linked to funding rounds.
7. Update pipeline job progress in the DB.
"""

import logging
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

import requests
from sqlalchemy.orm import Session

from app.core.db.base import SessionLocal
from app.models.company import Company
from app.models.funding import FundingRecord
from app.models.investors import FundingInvestor, Investor
from app.models.pipeline_jobs import PipelineJob
from app.services.company_enrichment import enrich_company_profile
from app.services.data_pipeline import funding_classifier, identity_resolver

logger = logging.getLogger(__name__)

SAM_ENTITY_URL = "https://api.sam.gov/entity-information/v3/entities"

# Source priority order for company profile merging (index 0 = highest priority)
SOURCE_PRIORITY = ["sam", "pitchbook", "sbir", "usaspending"]

DEFAULT_ENRICHMENT_SOURCES = ["sbir", "nsf", "usaspending", "census", "uspto"]


# ---------------------------------------------------------------------------
# Public: single-company pipeline
# ---------------------------------------------------------------------------


def run_pipeline_for_company(
    company_id: UUID,
    sources: Optional[List[str]] = None,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """
    Run the full data pipeline for one company.

    Returns a summary dict with status and counts.
    """
    close_db = db is None
    if db is None:
        db = SessionLocal()

    result: Dict[str, Any] = {
        "company_id": str(company_id),
        "status": "completed",
        "uei_resolved": False,
        "sam_updated": False,
        "enrichment": {},
        "funding_classified": 0,
        "errors": [],
    }

    try:
        company = db.query(Company).filter(Company.id == company_id).first()
        if not company:
            result["status"] = "not_found"
            result["errors"].append("Company not found")
            return result

        # ── Step 1: Resolve UEI ────────────────────────────────────────────
        if not _normalise_uei(company.uei):
            uei = _resolve_uei(company)
            if uei:
                company.uei = uei
                db.flush()
                result["uei_resolved"] = True

        # ── Step 2: Pull SAM.gov profile ───────────────────────────────────
        if _normalise_uei(company.uei):
            sam_updated = _update_company_from_sam(company, db)
            result["sam_updated"] = sam_updated

        db.commit()

        # ── Step 3: Multi-source enrichment ───────────────────────────────
        enrichment_sources = sources or DEFAULT_ENRICHMENT_SOURCES
        enrichment_result = enrich_company_profile(company_id, enrichment_sources)
        result["enrichment"] = enrichment_result

        # ── Step 4: Classify funding records ──────────────────────────────
        classified = _classify_company_funding(company_id, db)
        result["funding_classified"] = classified

        db.commit()

    except Exception as exc:
        db.rollback()
        logger.exception("Pipeline failed for company %s", company_id)
        result["status"] = "failed"
        result["errors"].append(str(exc))
    finally:
        if close_db:
            db.close()

    return result


# ---------------------------------------------------------------------------
# Public: batch pipeline (runs in background thread)
# ---------------------------------------------------------------------------


def run_pipeline_batch(
    job_id: UUID,
    company_ids: Optional[List[str]],
    sources: Optional[List[str]] = None,
) -> None:
    """
    Run the pipeline for multiple companies and update the PipelineJob record.

    Intended to be invoked from a background thread via FastAPI's BackgroundTasks.
    """
    db = SessionLocal()
    try:
        job = db.query(PipelineJob).filter(PipelineJob.id == job_id).first()
        if not job:
            logger.error("PipelineJob %s not found", job_id)
            return

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        db.commit()

        # Determine which companies to process
        if company_ids:
            ids = [UUID(cid) for cid in company_ids]
        else:
            ids = [row[0] for row in db.query(Company.id).all()]

        job.total_companies = len(ids)
        db.commit()

        company_results: List[Dict[str, Any]] = []
        processed = 0
        failed = 0

        for cid in ids:
            try:
                summary = run_pipeline_for_company(cid, sources)
                company_results.append(summary)
                if summary["status"] == "failed":
                    failed += 1
                else:
                    processed += 1
            except Exception as exc:
                logger.exception("Batch pipeline error for company %s", cid)
                company_results.append({"company_id": str(cid), "status": "failed", "error": str(exc)})
                failed += 1

            # Update progress every 10 companies
            if (processed + failed) % 10 == 0:
                job.processed_companies = processed
                job.failed_companies = failed
                db.commit()

        job.status = "completed" if failed == 0 else "failed"
        job.processed_companies = processed
        job.failed_companies = failed
        job.completed_at = datetime.now(timezone.utc)
        job.results_summary = {
            "total": len(ids),
            "processed": processed,
            "failed": failed,
        }
        db.commit()
        logger.info(
            "Pipeline batch %s complete: processed=%d failed=%d",
            job_id,
            processed,
            failed,
        )

    except Exception:
        db.rollback()
        logger.exception("Pipeline batch %s crashed", job_id)
        try:
            job = db.query(PipelineJob).filter(PipelineJob.id == job_id).first()
            if job:
                job.status = "failed"
                job.completed_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


def _resolve_uei(company: Company) -> Optional[str]:
    """Try DUNS-based then name-based UEI resolution."""
    # DUNS → UEI via SBIR
    if company.duns:
        uei = identity_resolver.resolve_uei_from_duns(company.duns)
        if uei:
            logger.info("Resolved UEI from DUNS for company '%s': %s", company.name, uei)
            return uei

    # Name-based SAM.gov search
    uei = identity_resolver.resolve_uei_from_name(
        company.name,
        website=company.website_url,
        city=company.city or company.hq_city,
        state=company.state or company.hq_state,
    )
    if uei:
        logger.info("Resolved UEI by name for company '%s': %s", company.name, uei)
    return uei


def _normalise_uei(uei: Optional[str]) -> Optional[str]:
    if not uei:
        return None
    normalised = "".join(ch for ch in uei.strip().upper() if ch.isalnum())
    return normalised or None


# ---------------------------------------------------------------------------
# SAM.gov company profile update
# ---------------------------------------------------------------------------


def _update_company_from_sam(company: Company, db: Session) -> bool:
    """
    Fetch entity data from SAM.gov and update the company record.

    Returns True if any field was updated.
    """
    api_key = os.getenv("SAM_API_KEY")
    if not api_key:
        logger.warning("SAM_API_KEY not set; skipping SAM profile update")
        return False

    uei = _normalise_uei(company.uei)
    if not uei:
        return False

    try:
        resp = requests.get(
            SAM_ENTITY_URL,
            params={
                "api_key": api_key,
                "ueiSAM": uei,
                "includeSections": "entityRegistration,coreData",
                "format": "json",
            },
            timeout=20,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        logger.exception("SAM.gov profile fetch failed for UEI %s", uei)
        return False

    entities = payload.get("entityData") or payload.get("data") or []
    if not entities:
        return False

    entity = entities[0]
    reg = entity.get("entityRegistration") or {}
    core = entity.get("coreData") or {}
    phys = core.get("physicalAddress") or {}
    mail = core.get("mailingAddress") or {}

    updated = False

    def _set(attr: str, value: Any) -> None:
        nonlocal updated
        if value and not getattr(company, attr, None):
            setattr(company, attr, value)
            updated = True

    # Legal name (highest priority)
    legal_name = reg.get("legalBusinessName") or reg.get("legalName")
    if legal_name:
        company.name = legal_name
        updated = True

    _set("dba_name", reg.get("dbaName"))
    _set("cage_code", reg.get("cageCode"))
    _set("entity_type_code", reg.get("entityTypeCode"))
    _set("uei", reg.get("ueiSAM"))

    # Address: prefer physical, fall back to mailing
    addr_src = phys if phys else mail
    address_line = " ".join(filter(None, [addr_src.get("addressLine1"), addr_src.get("addressLine2")])).strip()
    _set("address1", addr_src.get("addressLine1"))
    _set("address2", addr_src.get("addressLine2"))
    _set("city", addr_src.get("city"))
    _set("state", addr_src.get("stateOrProvinceCode"))
    _set("zip", addr_src.get("zipCode"))
    _set("hq_country", addr_src.get("countryCode"))

    _set("congressional_district", core.get("congressionalDistrict"))
    _set("website_url", reg.get("entityURL"))
    _set("incorporation_state", core.get("incorporationStateCode"))
    _set("incorporation_country", core.get("incorporationCountryCode"))
    _set("naics", _primary_naics(core))

    if updated:
        db.flush()

    return updated


def _primary_naics(core: Dict[str, Any]) -> Optional[str]:
    """Extract primary NAICS code from SAM core data."""
    naics_list = core.get("generalInformation", {}).get("naicsCodeList", [])
    if not naics_list:
        return None
    # The primary code is usually marked with isPrimary=True
    for entry in naics_list:
        if entry.get("isPrimary") or entry.get("primary"):
            return str(entry.get("naicsCode") or "")
    if naics_list:
        return str(naics_list[0].get("naicsCode") or "")
    return None


# ---------------------------------------------------------------------------
# Funding classification
# ---------------------------------------------------------------------------


def _classify_company_funding(company_id: UUID, db: Session) -> int:
    """
    Classify every unclassified (or reclassify all) funding records for a company.

    Returns the number of records updated.
    """
    records: List[FundingRecord] = (
        db.query(FundingRecord).filter(FundingRecord.company_id == company_id).all()
    )

    updated = 0
    for record in records:
        try:
            amount = record.amount_awarded or record.award_amount
            if amount is not None:
                amount = Decimal(str(amount))

            source, stage = funding_classifier.classify_funding(
                raw_source=record.funding_source,
                amount=amount,
                phase=record.phase,
                raw_type=record.funding_type,
                agency=record.agency,
                program=record.program,
            )

            changed = False
            if record.funding_source != source:
                record.funding_source = source
                changed = True
            if record.funding_stage != stage and stage is not None:
                record.funding_stage = stage
                changed = True
            if changed:
                updated += 1
        except Exception:
            logger.exception(
                "Funding classification failed for record %s", record.id
            )

    if updated:
        db.flush()

    return updated
