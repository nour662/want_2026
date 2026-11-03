"""
USAspending.gov contracts and grants, searched by UEI.

Loans (07/08), direct payments and insurance are excluded on purpose: they are
not funding for WATN reporting. Every returned award is re-checked against the
queried UEI because ``recipient_search_text`` is a text search and can return
other recipients.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from app.services.watn_pipeline import http
from app.services.watn_pipeline.matching import normalize_id
from app.services.watn_pipeline.models import FundingRecord
from app.services.watn_pipeline.normalize import clean_str, parse_amount, parse_date

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_URL = "https://www.usaspending.gov/award/"
CONTRACT_CODES = ["A", "B", "C", "D"]
GRANT_CODES = ["02", "03", "04", "05"]
FIELDS = [
    "Award ID", "Recipient Name", "Recipient UEI", "Start Date", "End Date", "Award Amount",
    "Awarding Agency", "Awarding Sub Agency", "Contract Award Type", "Award Type",
    "Funding Agency", "Funding Sub Agency", "Description", "generated_internal_id",
]
MAX_PAGES = 20


def _search(uei: str, codes: List[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        payload = {
            "filters": {
                "recipient_search_text": [uei],
                "award_type_codes": codes,
                "time_period": [{"start_date": "2007-10-01", "end_date": "2099-12-31"}],
            },
            "fields": FIELDS,
            "page": page,
            "limit": 100,
            "sort": "Award Amount",
            "order": "desc",
            "subawards": False,
        }
        data = http.post_json(SEARCH_URL, payload, timeout=60)
        results.extend(data.get("results") or [])
        if not (data.get("page_metadata") or {}).get("hasNext"):
            break
    return results


def fetch(uei: str) -> Tuple[List[FundingRecord], Dict[str, Any]]:
    """Return (funding records, recipient profile hints)."""
    uei = normalize_id(uei)
    records: List[FundingRecord] = []
    profile: Dict[str, Any] = {}
    for source, codes in (("usas_contracts", CONTRACT_CODES), ("usas_grants", GRANT_CODES)):
        for row in _search(uei, codes):
            if normalize_id(row.get("Recipient UEI")) != uei:
                continue  # text search hit a different recipient
            records.append(_to_funding(row, source))
            profile.setdefault("legal_name", clean_str(row.get("Recipient Name")))
    return records, profile


def _to_funding(row: Dict[str, Any], source: str) -> FundingRecord:
    internal_id = clean_str(row.get("generated_internal_id"))
    sub_agency = clean_str(row.get("Awarding Sub Agency"))
    agency = clean_str(row.get("Awarding Agency"))
    award_type = clean_str(row.get("Contract Award Type")) or clean_str(row.get("Award Type"))
    info_parts = [
        f"{label}: {clean_str(row.get(key))}"
        for label, key in (("Awarding Agency", "Awarding Agency"), ("Awarding Sub Agency", "Awarding Sub Agency"),
                           ("Funding Agency", "Funding Agency"), ("Award Type", "Award Type"),
                           ("Contract Award Type", "Contract Award Type"))
        if clean_str(row.get(key))
    ]
    description = clean_str(row.get("Description"))
    return FundingRecord(
        source=source,
        amount=parse_amount(row.get("Award Amount")),
        date_awarded=parse_date(row.get("Start Date")),
        end_date=parse_date(row.get("End Date")),
        award_number=clean_str(row.get("Award ID")),
        external_id=internal_id,
        title=description.title() if description and description.isupper() else description,
        investor=sub_agency or agency,
        raw_type=award_type or ("Grant" if source == "usas_grants" else "Contract"),
        program=description,
        source_url=AWARD_URL + internal_id if internal_id else None,
        additional_info="; ".join(info_parts) or None,
        recipient_name=clean_str(row.get("Recipient Name")),
        recipient_uei=normalize_id(row.get("Recipient UEI")),
    )
