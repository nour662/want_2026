import hashlib
import logging
import math
import time
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

import requests
from sqlalchemy.orm import Session

from app.core.cache import cache_get, cache_set
from app.core.db.base import SessionLocal
from app.models.company import Company
from app.models.company_enrichment import CompanyGeoEnrichment, EnrichmentApiFailureLog
from app.models.funding import FundingRecord
from app.models.patents import Patent

logger = logging.getLogger(__name__)

SBIR_BASE_URL = "https://api.www.sbir.gov/public/api"
NSF_AWARDS_URL = "https://api.nsf.gov/services/v1/awards.json"
USASPENDING_AWARDS_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
CENSUS_GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_GEOGRAPHY_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
PATENTSVIEW_QUERY_URL = "https://api.patentsview.org/patents/query"

CACHE_TTL_SECONDS = 60 * 60 * 24
MAX_NSF_RESULTS = 3000
NSF_RPP = 25

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
ENRICHMENT_SOURCES = ("sbir", "nsf", "usaspending", "census", "uspto")


def normalize_enrichment_sources(sources: Optional[Iterable[str]] = None) -> List[str]:
    if not sources:
        return list(ENRICHMENT_SOURCES)

    normalized_sources = []
    for source in sources:
        normalized = str(source).strip().lower()
        if normalized in ENRICHMENT_SOURCES and normalized not in normalized_sources:
            normalized_sources.append(normalized)
    return normalized_sources or list(ENRICHMENT_SOURCES)


def enrich_company_profile(company_id: UUID, sources: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    db = SessionLocal()
    requested_sources = normalize_enrichment_sources(sources)
    summary: Dict[str, Any] = {
        "company_id": str(company_id),
        "requested_sources": requested_sources,
        "skipped_sources": [],
        "funding_records_processed": 0,
        "patents_processed": 0,
        "status": "completed",
        "message": "Company enrichment completed.",
    }
    try:
        company = db.query(Company).filter(Company.id == company_id).first()
        if not company:
            logger.warning("Enrichment skipped: company %s not found", company_id)
            summary["status"] = "not_found"
            summary["message"] = "Company not found."
            return summary

        uei = _normalize_uei(company.uei)
        sbir_company = None
        sbir_awards: List[Dict[str, Any]] = []
        nsf_awards: List[Dict[str, Any]] = []
        usaspending_awards: List[Dict[str, Any]] = []
        usaspending_metadata: Dict[str, Any] = {}
        census_geo: Dict[str, Any] = {}
        patents: List[Dict[str, Any]] = []

        if "sbir" in requested_sources:
            if uei:
                sbir_company, sbir_awards = _fetch_sbir_data(db, company_id, uei)
            else:
                summary["skipped_sources"].append("sbir")

        if "nsf" in requested_sources:
            if uei:
                nsf_awards = _fetch_nsf_awards(db, company_id, uei)
            else:
                summary["skipped_sources"].append("nsf")

        if "usaspending" in requested_sources:
            if uei:
                usaspending_awards, usaspending_metadata = _fetch_usaspending_data(db, company_id, uei)
            else:
                summary["skipped_sources"].append("usaspending")

        if "census" in requested_sources:
            census_geo = _fetch_census_geographies(db, company_id, company)
            if not census_geo:
                summary["skipped_sources"].append("census")

        if "uspto" in requested_sources:
            if uei or company.name:
                patents = _fetch_uspto_patents(db, company_id, uei or "", company.name)
            else:
                summary["skipped_sources"].append("uspto")

        _apply_company_enrichment(
            company,
            sbir_company if "sbir" in requested_sources else None,
            usaspending_metadata if "usaspending" in requested_sources else {},
            census_geo if "census" in requested_sources else {},
        )

        normalized_funding = _normalize_funding_records(
            sbir_awards=sbir_awards,
            nsf_awards=nsf_awards,
            usaspending_awards=usaspending_awards,
        )
        if normalized_funding:
            _upsert_funding_records(db, company_id, normalized_funding)
        if patents:
            _upsert_patents(db, company_id, patents)
        if census_geo:
            _upsert_geographic_enrichment(db, company_id, census_geo)

        summary["funding_records_processed"] = len(normalized_funding)
        summary["patents_processed"] = len(patents)

        db.commit()
        logger.info(
            "Completed enrichment for company %s (uei=%s): funding=%s patents=%s",
            company_id,
            uei,
            len(normalized_funding),
            len(patents),
        )
        completed_sources = [source for source in requested_sources if source not in summary["skipped_sources"]]
        if completed_sources:
            summary["message"] = f"Updated company data from {', '.join(completed_sources)}."
        elif summary["skipped_sources"]:
            summary["message"] = "No enrichment sources could run with the company data currently available."
    except Exception:
        db.rollback()
        logger.exception("Company enrichment failed for company %s", company_id)
        summary["status"] = "failed"
        summary["message"] = "Company enrichment failed."
    finally:
        db.close()
    return summary


def enrich_companies_in_bulk(company_ids: Iterable[UUID]) -> None:
    for company_id in set(company_ids):
        enrich_company_profile(company_id)


def _normalize_uei(uei: Optional[str]) -> Optional[str]:
    if not uei:
        return None
    normalized = "".join(ch for ch in uei.strip().upper() if ch.isalnum())
    return normalized or None


def _cache_key(source: str, suffix: str) -> str:
    digest = hashlib.sha256(f"{source}:{suffix}".encode("utf-8")).hexdigest()
    return f"enrichment:{source}:{digest}"


def _cache_get_json(key: str) -> Optional[Any]:
    try:
        return cache_get(key)
    except Exception:
        logger.warning("Cache read failed for key %s", key, exc_info=True)
        return None


def _cache_set_json(key: str, value: Any, ttl: int = CACHE_TTL_SECONDS) -> None:
    try:
        cache_set(key, value, expire=ttl)
    except Exception:
        logger.warning("Cache write failed for key %s", key, exc_info=True)


def _log_api_failure(
    db: Session,
    company_id: Optional[UUID],
    source: str,
    endpoint: str,
    error_message: str,
    status_code: Optional[int] = None,
    attempt: Optional[int] = None,
) -> None:
    del db
    log_db = SessionLocal()
    try:
        log_db.add(
            EnrichmentApiFailureLog(
                company_id=company_id,
                source=source,
                endpoint=endpoint,
                status_code=status_code,
                error_message=error_message[:4000],
                attempt=attempt,
            )
        )
        log_db.commit()
    except Exception:
        log_db.rollback()
        logger.exception("Failed to persist enrichment API failure log")
    finally:
        log_db.close()


def _request_with_retries(
    db: Session,
    company_id: UUID,
    source: str,
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_payload: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    max_attempts: int = 3,
) -> Optional[requests.Response]:
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(
                method,
                url,
                params=params,
                json=json_payload,
                timeout=timeout,
                headers={"Accept": "application/json"},
            )
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < max_attempts:
                time.sleep(0.4 * (2 ** (attempt - 1)))
                continue
            if response.status_code >= 400:
                _log_api_failure(
                    db,
                    company_id,
                    source,
                    url,
                    f"HTTP {response.status_code}: {response.text[:1000]}",
                    status_code=response.status_code,
                    attempt=attempt,
                )
                return None
            return response
        except requests.RequestException as exc:
            _log_api_failure(
                db,
                company_id,
                source,
                url,
                f"Request exception: {exc}",
                attempt=attempt,
            )
            if attempt < max_attempts:
                time.sleep(0.4 * (2 ** (attempt - 1)))
                continue
            return None
    return None


def _safe_json(response: Optional[requests.Response]) -> Optional[Dict[str, Any]]:
    if not response:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _fetch_sbir_data(db: Session, company_id: UUID, uei: str) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    cache_key = _cache_key("sbir", uei)
    cached = _cache_get_json(cache_key)
    if isinstance(cached, dict):
        return cached.get("company"), cached.get("awards", [])

    company_payload = None
    company_response = _request_with_retries(
        db,
        company_id,
        "sbir",
        "GET",
        f"{SBIR_BASE_URL}/firm",
        params={"uei": uei, "rows": 1, "start": 0},
    )
    parsed_company = _extract_list(_safe_json(company_response))
    if parsed_company:
        company_payload = parsed_company[0]

    all_awards: List[Dict[str, Any]] = []
    start = 0
    rows = 200
    while True:
        awards_response = _request_with_retries(
            db,
            company_id,
            "sbir",
            "GET",
            f"{SBIR_BASE_URL}/awards",
            params={"uei": uei, "rows": rows, "start": start},
        )
        page_awards = _extract_list(_safe_json(awards_response))
        if not page_awards:
            break
        all_awards.extend(page_awards)
        if len(page_awards) < rows:
            break
        start += rows
        if start >= 5000:
            break

    filtered_awards = [
        award
        for award in all_awards
        if str(award.get("agency", "")).strip().upper() != "NSF"
    ]

    result = {"company": company_payload, "awards": filtered_awards}
    _cache_set_json(cache_key, result)
    return company_payload, filtered_awards


def _fetch_nsf_awards(db: Session, company_id: UUID, uei: str) -> List[Dict[str, Any]]:
    cache_key = _cache_key("nsf", uei)
    cached = _cache_get_json(cache_key)
    if isinstance(cached, list):
        return cached

    awards: List[Dict[str, Any]] = []
    offset = 0

    while offset < MAX_NSF_RESULTS:
        response = _request_with_retries(
            db,
            company_id,
            "nsf",
            "GET",
            NSF_AWARDS_URL,
            params={
                "ueiNumber": uei,
                "rpp": NSF_RPP,
                "offset": offset,
            },
        )
        payload = _safe_json(response) or {}
        response_obj = payload.get("response", payload)

        service_notifications = response_obj.get("serviceNotification")
        if service_notifications:
            message = " | ".join(str(item) for item in service_notifications)
            _log_api_failure(db, company_id, "nsf", NSF_AWARDS_URL, f"serviceNotification: {message}")
            break

        page_awards = response_obj.get("award") or []
        if not isinstance(page_awards, list):
            page_awards = []
        if not page_awards:
            break

        awards.extend(page_awards)
        if len(page_awards) < NSF_RPP:
            break
        offset += NSF_RPP

    _cache_set_json(cache_key, awards)
    return awards


def _fetch_usaspending_data(db: Session, company_id: UUID, uei: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    cache_key = _cache_key("usaspending", uei)
    cached = _cache_get_json(cache_key)
    if isinstance(cached, dict):
        return cached.get("awards", []), cached.get("metadata", {})

    all_awards: List[Dict[str, Any]] = []
    metadata: Dict[str, Any] = {}
    page = 1
    limit = 100

    while True:
        payload = {
            "fields": [
                "Award ID",
                "Recipient Name",
                "Action Date",
                "Award Amount",
                "Award Type",
                "Awarding Agency",
                "Awarding Sub Agency",
                "Description",
                "UEI",
                "Internal ID",
            ],
            "filters": {
                "recipient_uei": [uei],
                "recipient_search_text": [uei],
                "award_type_codes": ["02", "03", "07", "08"],
            },
            "page": page,
            "limit": limit,
            "sort": "Award Amount",
            "order": "desc",
            "subawards": False,
        }

        response = _request_with_retries(
            db,
            company_id,
            "usaspending",
            "POST",
            USASPENDING_AWARDS_URL,
            json_payload=payload,
        )
        parsed = _safe_json(response)
        if not isinstance(parsed, dict):
            break

        page_awards = parsed.get("results") or []
        if not isinstance(page_awards, list):
            page_awards = []
        if not page_awards:
            break

        all_awards.extend(page_awards)

        page_meta = parsed.get("page_metadata") or {}
        metadata = {
            "hasNext": page_meta.get("hasNext", False),
            "total": page_meta.get("total", len(all_awards)),
        }
        if not page_meta.get("hasNext"):
            break

        page += 1
        if page > 50:
            break

    result = {"awards": all_awards, "metadata": metadata}
    _cache_set_json(cache_key, result)
    return all_awards, metadata


def _compose_company_address(company: Company) -> Optional[str]:
    parts = [
        company.address1,
        company.address2,
        company.hq_city or company.city,
        company.hq_state or company.state,
        company.zip,
    ]
    address = ", ".join(part.strip() for part in parts if part and part.strip())
    return address or None


def _fetch_census_geographies(db: Session, company_id: UUID, company: Company) -> Dict[str, Any]:
    address = _compose_company_address(company)
    if not address:
        return {}

    cache_key = _cache_key("census", address)
    cached = _cache_get_json(cache_key)
    if isinstance(cached, dict):
        return cached

    geocode_response = _request_with_retries(
        db,
        company_id,
        "census",
        "GET",
        CENSUS_GEOCODE_URL,
        params={
            "address": address,
            "benchmark": "Public_AR_Current",
            "format": "json",
        },
    )
    geocode_payload = _safe_json(geocode_response) or {}
    address_matches = (
        geocode_payload.get("result", {}).get("addressMatches", [])
        if isinstance(geocode_payload, dict)
        else []
    )
    if not address_matches:
        return {}

    first_match = address_matches[0]
    coords = first_match.get("coordinates") or {}
    x = coords.get("x")
    y = coords.get("y")
    if x is None or y is None:
        return {}

    geography_response = _request_with_retries(
        db,
        company_id,
        "census",
        "GET",
        CENSUS_GEOGRAPHY_URL,
        params={
            "x": x,
            "y": y,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        },
    )
    geography_payload = _safe_json(geography_response) or {}
    geographies = geography_payload.get("result", {}).get("geographies", {})

    def first_geo(name_fragment: str) -> Dict[str, Any]:
        for geo_name, items in geographies.items():
            if name_fragment.lower() in geo_name.lower() and isinstance(items, list) and items:
                return items[0]
        return {}

    congressional = first_geo("Congressional District")
    state_upper = first_geo("State Legislative Districts - Upper")
    state_lower = first_geo("State Legislative Districts - Lower")
    county = first_geo("Counties")
    tract = first_geo("Census Tracts")
    place = first_geo("Places")

    result = {
        "normalized_address": first_match.get("matchedAddress"),
        "congressional_district": congressional.get("CD118") or congressional.get("CD119") or congressional.get("CD116"),
        "state_legislative_upper": state_upper.get("SLDU"),
        "state_legislative_lower": state_lower.get("SLDL"),
        "county_fips": county.get("COUNTY"),
        "place_fips": place.get("PLACE"),
        "census_tract": tract.get("TRACT"),
        "geoid": tract.get("GEOID") or county.get("GEOID"),
    }

    _cache_set_json(cache_key, result)
    return result


def _fetch_uspto_patents(db: Session, company_id: UUID, uei: str, company_name: Optional[str]) -> List[Dict[str, Any]]:
    cache_key = _cache_key("uspto", f"{uei}:{company_name or ''}")
    cached = _cache_get_json(cache_key)
    if isinstance(cached, list):
        return cached

    queries = []
    # Primary attempt by UEI field (not always available in public patent datasets).
    queries.append({"_eq": {"assignee_organization_uei": uei}})

    if company_name:
        normalized_name = company_name.strip()
        queries.append({"_contains": {"assignee_organization": normalized_name}})

    patents: List[Dict[str, Any]] = []
    for query in queries:
        response = _request_with_retries(
            db,
            company_id,
            "uspto",
            "POST",
            PATENTSVIEW_QUERY_URL,
            json_payload={
                "q": query,
                "f": [
                    "patent_number",
                    "patent_title",
                    "patent_date",
                    "patent_type",
                    "patent_kind",
                    "patent_num_claims",
                    "patent_processing_time",
                    "assignee_organization",
                    "assignee_lastknown_city",
                    "assignee_lastknown_state",
                    "application_number",
                ],
                "o": {"per_page": 100, "page": 1},
            },
        )
        payload = _safe_json(response)
        page_patents = payload.get("patents", []) if isinstance(payload, dict) else []
        if isinstance(page_patents, list) and page_patents:
            patents = page_patents
            break

    _cache_set_json(cache_key, patents)
    return patents


def _extract_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _first_nonempty(record: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    if "T" in text:
        head = text.split("T", 1)[0]
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(head, fmt).date()
            except ValueError:
                continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _apply_company_enrichment(
    company: Company,
    sbir_company: Optional[Dict[str, Any]],
    usaspending_metadata: Dict[str, Any],
    census_geo: Dict[str, Any],
) -> None:
    if sbir_company:
        employee_count = _parse_float(_first_nonempty(sbir_company, ["number_employees", "employees"]))
        company.website_url = company.website_url or _first_nonempty(sbir_company, ["company_url", "website", "web_site"])
        company.address1 = company.address1 or _first_nonempty(sbir_company, ["address1", "street1", "address"])
        company.address2 = company.address2 or _first_nonempty(sbir_company, ["address2", "street2"])
        company.city = company.city or _first_nonempty(sbir_company, ["city"])
        company.state = company.state or _first_nonempty(sbir_company, ["state", "state_code"])
        company.zip = company.zip or _first_nonempty(sbir_company, ["zip", "zipcode", "postal_code"])
        if company.number_employees is None and employee_count is not None and employee_count > 0:
            company.number_employees = int(employee_count)

    if census_geo:
        company.hq_city = company.hq_city or company.city
        company.hq_state = company.hq_state or company.state

    # Keep as metadata touchpoint for now; useful for quality checks.
    if usaspending_metadata and not company.description:
        total = usaspending_metadata.get("total")
        if isinstance(total, int) and total > 0:
            company.description = f"Enriched from USAspending ({total} related awards found)."


def _normalize_funding_records(
    *,
    sbir_awards: List[Dict[str, Any]],
    nsf_awards: List[Dict[str, Any]],
    usaspending_awards: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []

    for award in sbir_awards:
        agency = str(_first_nonempty(award, ["agency", "agency_name"]) or "")
        phase = str(_first_nonempty(award, ["phase", "program_phase"]) or "")
        funding_source = "STTR" if "STTR" in phase.upper() else "SBIR"
        normalized.append(
            {
                "source": "sbir",
                "external_id": _first_nonempty(award, ["id", "award_id", "firm_award_id"]),
                "award_number": _first_nonempty(award, ["award_number", "contract"]),
                "agency": agency,
                "program_name": _first_nonempty(award, ["program", "topic"]),
                "award_title": _first_nonempty(award, ["title", "award_title"]),
                "amount": _parse_float(_first_nonempty(award, ["award_amount", "amount", "award_amount_usd"])),
                "date_awarded": _parse_date(_first_nonempty(award, ["award_date", "proposal_award_date", "start_date"])),
                "contract_end_date": _parse_date(_first_nonempty(award, ["contract_end_date", "end_date"])),
                "abstract": _first_nonempty(award, ["abstract", "description"]),
                "source_url": _first_nonempty(award, ["award_link", "source_url"]),
                "funding_source": funding_source,
                "funding_type": "Federal",
            }
        )

    for award in nsf_awards:
        normalized.append(
            {
                "source": "nsf",
                "external_id": _first_nonempty(award, ["id", "awardId"]),
                "award_number": _first_nonempty(award, ["awardNumber"]),
                "agency": "NSF",
                "program_name": _first_nonempty(award, ["fundProgramName", "fundProgramCode"]),
                "award_title": _first_nonempty(award, ["title"]),
                "amount": _parse_float(_first_nonempty(award, ["fundsObligatedAmt", "estimatedTotalAmt"])),
                "date_awarded": _parse_date(_first_nonempty(award, ["date", "startDate"])),
                "contract_end_date": _parse_date(_first_nonempty(award, ["expDate"])),
                "abstract": _first_nonempty(award, ["abstractText"]),
                "source_url": None,
                "funding_source": "Federal",
                "funding_type": "Federal",
            }
        )

    for award in usaspending_awards:
        award_type = str(_first_nonempty(award, ["Award Type"]) or "")
        normalized.append(
            {
                "source": "usaspending",
                "external_id": _first_nonempty(award, ["Internal ID", "generated_internal_id"]),
                "award_number": _first_nonempty(award, ["Award ID", "award_id"]),
                "agency": _first_nonempty(award, ["Awarding Agency", "awarding_agency"]),
                "program_name": _first_nonempty(award, ["Awarding Sub Agency", "awarding_sub_agency"]),
                "award_title": _first_nonempty(award, ["Description", "description"]),
                "amount": _parse_float(_first_nonempty(award, ["Award Amount", "total_obligation"])),
                "date_awarded": _parse_date(_first_nonempty(award, ["Action Date", "action_date"])),
                "contract_end_date": None,
                "abstract": _first_nonempty(award, ["Description", "description"]),
                "source_url": None,
                "funding_source": "Federal",
                "funding_type": award_type or "Federal",
            }
        )

    deduped: Dict[str, Dict[str, Any]] = {}
    for record in normalized:
        key_bits = [
            str(record.get("external_id") or "").strip(),
            str(record.get("award_number") or "").strip(),
            str(record.get("agency") or "").strip(),
            str(record.get("award_title") or "").strip().lower(),
            str(record.get("date_awarded") or ""),
            str(record.get("amount") or ""),
        ]
        raw_key = "|".join(key_bits)
        digest = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()
        if digest not in deduped:
            deduped[digest] = record

    return list(deduped.values())


def _upsert_funding_records(db: Session, company_id: UUID, records: List[Dict[str, Any]]) -> None:
    for item in records:
        external_id = item.get("external_id")
        award_number = item.get("award_number")
        agency = item.get("agency")

        existing = None
        if external_id:
            existing = (
                db.query(FundingRecord)
                .filter(FundingRecord.company_id == company_id, FundingRecord.funding_id == str(external_id))
                .first()
            )
        if not existing and award_number:
            existing = (
                db.query(FundingRecord)
                .filter(
                    FundingRecord.company_id == company_id,
                    FundingRecord.award_number == str(award_number),
                    FundingRecord.agency == (str(agency) if agency else None),
                )
                .first()
            )

        target = existing or FundingRecord(company_id=company_id)
        target.funding_id = str(external_id) if external_id else target.funding_id
        target.award_number = str(award_number) if award_number else target.award_number
        target.award_title = item.get("award_title") or target.award_title
        target.agency = str(agency) if agency else target.agency
        target.program_name = item.get("program_name") or target.program_name
        target.abstract = item.get("abstract") or target.abstract
        target.source_url = item.get("source_url") or target.source_url
        target.funding_source = item.get("funding_source") or target.funding_source
        target.funding_type = item.get("funding_type") or target.funding_type
        target.funder_name = target.funder_name or target.agency

        amount = item.get("amount")
        if amount is not None and math.isfinite(float(amount)):
            target.amount_awarded = float(amount)
            target.award_amount = float(amount)

        if item.get("date_awarded"):
            target.date_awarded = item["date_awarded"]
        if item.get("contract_end_date"):
            target.contract_end_date = item["contract_end_date"]

        if not existing:
            db.add(target)


def _upsert_patents(db: Session, company_id: UUID, patents: List[Dict[str, Any]]) -> None:
    for item in patents:
        patent_number = _first_nonempty(item, ["patent_number"])
        application_number = _first_nonempty(item, ["application_number"])

        existing = None
        if patent_number:
            existing = (
                db.query(Patent)
                .filter(Patent.company_id == company_id, Patent.patent_number == str(patent_number))
                .first()
            )
        if not existing and application_number:
            existing = (
                db.query(Patent)
                .filter(Patent.company_id == company_id, Patent.application_number == str(application_number))
                .first()
            )

        title = _first_nonempty(item, ["patent_title"]) or "Untitled Patent"
        target = existing or Patent(company_id=company_id, title=str(title))

        target.title = str(title)
        target.patent_number = str(patent_number) if patent_number else target.patent_number
        target.application_number = str(application_number) if application_number else target.application_number
        target.status = _first_nonempty(item, ["patent_kind", "patent_type"]) or target.status
        target.filing_date = _parse_date(_first_nonempty(item, ["patent_date"])) or target.filing_date
        target.assignee_name = _first_nonempty(item, ["assignee_organization"]) or target.assignee_name

        if not existing:
            db.add(target)


def _upsert_geographic_enrichment(db: Session, company_id: UUID, geo: Dict[str, Any]) -> None:
    if not geo:
        return

    existing = db.query(CompanyGeoEnrichment).filter(CompanyGeoEnrichment.company_id == company_id).first()
    target = existing or CompanyGeoEnrichment(company_id=company_id)

    target.normalized_address = geo.get("normalized_address") or target.normalized_address
    target.congressional_district = geo.get("congressional_district") or target.congressional_district
    target.state_legislative_upper = geo.get("state_legislative_upper") or target.state_legislative_upper
    target.state_legislative_lower = geo.get("state_legislative_lower") or target.state_legislative_lower
    target.county_fips = geo.get("county_fips") or target.county_fips
    target.place_fips = geo.get("place_fips") or target.place_fips
    target.census_tract = geo.get("census_tract") or target.census_tract
    target.geoid = geo.get("geoid") or target.geoid

    if not existing:
        db.add(target)
