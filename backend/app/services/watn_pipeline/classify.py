"""
Funding source/stage classification using what each source actually knows,
falling back to the shared keyword classifier.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from app.services.data_pipeline import funding_classifier
from app.services.watn_pipeline.models import FundingRecord

_SBIR_RE = re.compile(r"\b(sbir|sttr)\b", re.I)
_PHASE_RE = re.compile(r"\bphase\s*(iii|ii|i|1|2|3)\b", re.I)
_CONTRACT_TYPES = (
    ("definitive contract", "Definitive Contract"),
    ("purchase order", "Purchase Order"),
    ("delivery order", "Delivery Order"),
    ("bpa call", "Delivery Order"),
    ("cooperative agreement", "Cooperative Agreement"),
)


def _phase_from_text(text: str) -> Optional[str]:
    match = _PHASE_RE.search(text or "")
    if not match:
        return None
    return {"1": "I", "2": "II", "3": "III"}.get(match.group(1), match.group(1).upper())


def classify(record: FundingRecord) -> FundingRecord:
    text = " ".join(filter(None, [record.program, record.title, record.raw_type, record.additional_info]))
    source = record.funding_source

    if source in ("SBIR", "STTR", "TEDCO"):
        pass
    elif _SBIR_RE.search(text):
        source = "STTR" if re.search(r"\bsttr\b", text, re.I) else "SBIR"
        record.phase = record.phase or _phase_from_text(text)
    elif record.source == "nsf" and re.search(r"i-?corps", text, re.I):
        source = "NSF I-Corps"
    elif record.source in ("usas_contracts", "usas_grants"):
        lowered = (record.raw_type or "").lower()
        source = next((label for needle, label in _CONTRACT_TYPES if needle in lowered), None)
        if source is None:
            source = "Grant" if record.source == "usas_grants" else "Other"
    elif record.source == "nsf":
        source = "Grant"
    else:
        source = funding_classifier.classify_funding_source(
            raw_source=record.funding_source, raw_type=record.raw_type, program=record.program
        )

    record.funding_source = source
    if not record.funding_stage:
        amount = Decimal(str(record.amount)) if record.amount is not None else None
        record.funding_stage = funding_classifier.classify_funding_stage(source, amount=amount, phase=record.phase)
    return record
