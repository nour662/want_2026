"""NSF awards, searched by UEI (the NSF API offset is 1-based)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.services.watn_pipeline import http
from app.services.watn_pipeline.matching import normalize_id
from app.services.watn_pipeline.models import FundingRecord
from app.services.watn_pipeline.normalize import clean_str, parse_amount, parse_date

logger = logging.getLogger(__name__)

NSF_URL = "https://api.nsf.gov/services/v1/awards.json"
PRINT_FIELDS = ",".join([
    "id", "title", "awardeeName", "awardeeUEI", "ueiNumber", "awardeeStateCode",
    "fundsObligatedAmt", "estimatedTotalAmt", "date", "startDate", "expDate", "pdPIName",
    "fundProgramName", "agency", "transType",
])
RPP = 25
MAX_RESULTS = 1000


def fetch(uei: str) -> List[FundingRecord]:
    uei = normalize_id(uei)
    records: List[FundingRecord] = []
    offset = 1
    while offset <= MAX_RESULTS:
        data = http.get_json(
            NSF_URL,
            params={"ueiNumber": uei, "printFields": PRINT_FIELDS, "offset": offset, "rpp": RPP},
            timeout=30,
        )
        response = data.get("response") or {}
        if response.get("serviceNotification"):
            raise RuntimeError(f"NSF API notification: {response['serviceNotification']}")
        awards: List[Dict[str, Any]] = response.get("award") or []
        for award in awards:
            if normalize_id(award.get("ueiNumber") or award.get("awardeeUEI")) == uei:
                records.append(_to_funding(award))
        if len(awards) < RPP:
            break
        offset += RPP
    return records


def _to_funding(award: Dict[str, Any]) -> FundingRecord:
    award_id = clean_str(award.get("id"))
    program = clean_str(award.get("fundProgramName"))
    pi = clean_str(award.get("pdPIName"))
    info = "; ".join(p for p in (f"Program: {program}" if program else "", f"PI: {pi}" if pi else "") if p)
    return FundingRecord(
        source="nsf",
        amount=parse_amount(award.get("fundsObligatedAmt")) or parse_amount(award.get("estimatedTotalAmt")),
        date_awarded=parse_date(award.get("startDate")) or parse_date(award.get("date")),
        end_date=parse_date(award.get("expDate")),
        award_number=award_id,
        external_id=award_id,
        title=clean_str(award.get("title")),
        investor="National Science Foundation",
        program=program,
        raw_type=clean_str(award.get("transType")) or "Grant",
        source_url=f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={award_id}" if award_id else None,
        additional_info=info or None,
        recipient_name=clean_str(award.get("awardeeName")),
        recipient_uei=normalize_id(award.get("ueiNumber") or award.get("awardeeUEI")),
    )
