"""
Cross-source merging.

Funding: records from SBIR, USAspending, NSF and TEDCO are clustered with a
union-find over three rules, then each cluster is collapsed into one record
using per-field source priorities. Provenance (which sources contributed) and
disagreements between sources are kept on the merged record.

    1. Same normalised award number (W911QX-24-P-0155 == W911QX24P0155).
    1b. ...or the same number with an agency prefix (DOCSB134112SE0595).
    2. Different sources, amount within 2% and award dates within 45 days
       (same year when one side only knows the award year).
    3. Never merge two records from the same source that carry different
       award numbers (two Phase I awards on the same day are two awards).

Company profile: each field is taken from the highest-priority source that has
a value; conflicts on key fields are reported for review.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from app.services.watn_pipeline.matching import extract_domain, normalize_id, normalize_state
from app.services.watn_pipeline.models import FieldValue, FundingRecord

AMOUNT_TOLERANCE = 0.02
DATE_TOLERANCE_DAYS = 45
AMOUNT_CONFLICT = 0.10

FUNDING_PRIORITIES: Dict[str, Sequence[str]] = {
    "amount": ("usas_contracts", "usas_grants", "nsf", "sbir", "tedco"),
    "date_awarded": ("usas_contracts", "usas_grants", "nsf", "sbir", "tedco"),
    "end_date": ("usas_contracts", "usas_grants", "nsf", "sbir", "tedco"),
    "award_number": ("sbir", "nsf", "usas_contracts", "usas_grants", "tedco"),
    "external_id": ("usas_contracts", "usas_grants", "nsf", "sbir", "tedco"),
    "title": ("sbir", "nsf", "usas_grants", "usas_contracts", "tedco"),
    "investor": ("sbir", "nsf", "usas_grants", "usas_contracts", "tedco"),
    "phase": ("sbir", "nsf", "usas_contracts", "usas_grants", "tedco"),
    "program": ("sbir", "nsf", "tedco", "usas_grants", "usas_contracts"),
    "raw_type": ("usas_contracts", "usas_grants", "sbir", "nsf", "tedco"),
    "funding_source": ("sbir", "tedco", "nsf", "usas_grants", "usas_contracts"),
    "funding_stage": ("sbir", "tedco", "nsf", "usas_grants", "usas_contracts"),
    "source_url": ("usas_contracts", "usas_grants", "nsf", "tedco", "sbir"),
    "recipient_name": ("sbir", "usas_contracts", "usas_grants", "nsf", "tedco"),
    "recipient_uei": ("sbir", "usas_contracts", "usas_grants", "nsf", "tedco"),
}

PROFILE_PRIORITIES: Dict[str, Sequence[str]] = {
    "legal_name": ("sam", "sbir", "usaspending"),
    "uei": ("input", "sam", "sbir"),
    "duns": ("input", "sbir"),
    "cage": ("sam",),
    "website": ("sam", "input", "sbir"),
    "address1": ("sam", "sbir"),
    "address2": ("sam", "sbir"),
    "city": ("sam", "sbir", "input"),
    "state": ("sam", "sbir", "input"),
    "zip": ("sam", "sbir"),
    "country": ("sam",),
    "congressional_district": ("sam",),
    "incorporation_state": ("sam",),
    "incorporation_country": ("sam",),
    "registration_date": ("sam",),
    "naics": ("sam",),
    "active": ("sam",),
    "employees": ("sbir",),
    "woman_owned": ("sbir",),
    "hubzone_owned": ("sbir",),
    "disadvantaged": ("sbir",),
    "university": ("input",),
}


# ── Funding ────────────────────────────────────────────────────────────


def award_key(value: Optional[str]) -> Optional[str]:
    key = normalize_id(value)
    return key if key and len(key) >= 5 else None


def _amount_close(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(1.0, AMOUNT_TOLERANCE * max(abs(a), abs(b)))


def awards_equal(ka: Optional[str], kb: Optional[str]) -> bool:
    """Equal, or one is the other with an agency prefix (DOCSB134112SE0595 vs SB134112SE0595)."""
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((ka, kb), key=len)
    return len(short) >= 8 and long_.endswith(short)


def _year_only(rec: FundingRecord) -> bool:
    return any(r.startswith("award date estimated") for r in rec.review_reasons)


def _same_award(a: FundingRecord, b: FundingRecord) -> bool:
    ka, kb = award_key(a.award_number), award_key(b.award_number)
    if awards_equal(ka, kb):
        return True
    if a.source == b.source:
        if ka and kb:
            return False
        # Same source, one record missing an award number: require exact match.
        return a.date_awarded == b.date_awarded and _amount_close(a.amount, b.amount) and a.amount is not None
    if a.date_awarded is None or b.date_awarded is None or not _amount_close(a.amount, b.amount):
        return False
    if _year_only(a) or _year_only(b):
        return a.date_awarded.year == b.date_awarded.year
    return abs((a.date_awarded - b.date_awarded).days) <= DATE_TOLERANCE_DAYS


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def merge_funding(records: List[FundingRecord]) -> List[FundingRecord]:
    n = len(records)
    uf = _UnionFind(n)
    by_award: Dict[str, int] = {}
    for i, rec in enumerate(records):
        key = award_key(rec.award_number)
        if key:
            if key in by_award:
                uf.union(i, by_award[key])
            else:
                by_award[key] = i
    for i in range(n):
        for j in range(i + 1, n):
            if uf.find(i) != uf.find(j) and _same_award(records[i], records[j]):
                uf.union(i, j)

    clusters: Dict[int, List[FundingRecord]] = {}
    for i, rec in enumerate(records):
        clusters.setdefault(uf.find(i), []).append(rec)
    merged = [_collapse(group) for group in clusters.values()]
    merged.sort(key=lambda r: (r.date_awarded is None, r.date_awarded or date.min))
    return merged


def _pick(group: List[FundingRecord], field_name: str) -> Any:
    order = FUNDING_PRIORITIES.get(field_name, ())
    ranked = sorted(group, key=lambda r: order.index(r.source) if r.source in order else len(order))
    for rec in ranked:
        value = getattr(rec, field_name)
        if value not in (None, ""):
            return value
    return None


def _collapse(group: List[FundingRecord]) -> FundingRecord:
    if len(group) == 1:
        rec = group[0]
        rec.sources = rec.sources or [rec.source]
        return rec

    base = FundingRecord(source=group[0].source)
    for field_name in FUNDING_PRIORITIES:
        setattr(base, field_name, _pick(group, field_name))
    # Primary source = the one that supplied the amount (what Salesforce reports on).
    base.source = next((r.source for r in group if r.amount == base.amount), group[0].source)
    base.sources = sorted({s for r in group for s in (r.sources or [r.source])})
    base.merged_count = sum(r.merged_count for r in group)
    base.confidence = max(r.confidence for r in group)
    base.match_method = "; ".join(sorted({r.match_method for r in group if r.match_method}))

    infos: List[str] = []
    for rec in group:
        for part in (rec.additional_info or "").split("; "):
            if part and part not in infos:
                infos.append(part)
    base.additional_info = "; ".join(infos) or None

    reasons: List[str] = []
    date_source = next((r for r in group if r.date_awarded == base.date_awarded), None)
    for rec in group:
        for reason in rec.review_reasons:
            if reason.startswith("award date estimated") and rec is not date_source:
                continue
            if reason not in reasons:
                reasons.append(reason)
    amounts = sorted({r.amount for r in group if r.amount})
    if len(amounts) > 1 and (amounts[-1] - amounts[0]) > AMOUNT_CONFLICT * amounts[-1]:
        reasons.append(
            "sources disagree on amount: "
            + ", ".join(f"{r.source}=${r.amount:,.0f}" for r in group if r.amount)
        )
    base.review_reasons = reasons
    return base


# ── Company profile ────────────────────────────────────────────────────


def merge_profile(profiles: Dict[str, Dict[str, Any]]) -> Dict[str, FieldValue]:
    merged: Dict[str, FieldValue] = {}
    for field_name, order in PROFILE_PRIORITIES.items():
        for source in order:
            value = (profiles.get(source) or {}).get(field_name)
            if value not in (None, "", []):
                merged[field_name] = FieldValue(value=value, source=source)
                break
    return merged


def profile_conflicts(profiles: Dict[str, Dict[str, Any]]) -> List[str]:
    """Disagreements on identity-bearing fields between sources."""
    conflicts: List[str] = []
    checks = (
        ("state", normalize_state),
        ("website", extract_domain),
        ("uei", normalize_id),
    )
    for field_name, norm in checks:
        values = {
            source: norm(p.get(field_name))
            for source, p in profiles.items()
            if p and p.get(field_name) and norm(p.get(field_name))
        }
        if len(set(values.values())) > 1:
            conflicts.append(
                f"{field_name} differs across sources: " + ", ".join(f"{s}={v}" for s, v in sorted(values.items()))
            )
    return conflicts
