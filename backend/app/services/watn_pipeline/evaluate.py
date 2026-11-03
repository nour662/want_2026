"""
Record-level quality checks.

A funding record is *publishable* (goes to funding_info.csv and, later, to
Salesforce) only when we are confident it belongs to the company and it has
the fields a funding record needs. Everything else goes to the review queue
with the reasons attached, instead of being silently dropped or silently kept.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from app.services.watn_pipeline.matching import PROBABLE
from app.services.watn_pipeline.models import FundingRecord

LARGE_AWARD = 25_000_000

BLOCKING_PREFIXES = (
    "amount missing",
    "non-positive amount",
    "award date missing",
    "identity not confirmed",
    "TEDCO mention needs review",
    "TEDCO article names company but no amount",
    "TEDCO amount",
)


def evaluate_funding(record: FundingRecord, cohort_date: Optional[date], today: Optional[date] = None) -> FundingRecord:
    today = today or date.today()
    reasons = list(record.review_reasons)

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if record.amount is None:
        if record.source != "tedco":
            add("amount missing")
    elif record.amount <= 0:
        add("non-positive amount (likely a de-obligation)")
    elif record.amount >= LARGE_AWARD:
        add("unusually large award; verify recipient")

    if record.date_awarded is None:
        add("award date missing")
    elif record.date_awarded > today:
        add("award date is in the future")

    if record.confidence < PROBABLE:
        add(f"identity not confirmed (confidence {record.confidence:.0f})")

    if cohort_date and record.date_awarded:
        record.post_icorps = record.date_awarded >= cohort_date
    record.review_reasons = reasons
    return record


def is_publishable(record: FundingRecord) -> bool:
    return not any(r.startswith(BLOCKING_PREFIXES) for r in record.review_reasons)
