"""
SBIR/STTR awards from SBIR.gov's public bulk award file.

The SBIR.gov JSON API currently answers 403 to scripted clients, which is why
the old WATN scraper drove Selenium. The bulk CSV is official, complete and
lets us look up thousands of companies in memory, so it is loaded once per run
(and re-downloaded when older than ``max_age_days``).

The index doubles as an identity source: it maps DUNS -> UEI and gives every
SBIR firm's website/state for name matching.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.watn_pipeline import http
from app.services.watn_pipeline.matching import name_similarity, normalize_duns, normalize_id, normalize_name
from app.services.watn_pipeline.models import FundingRecord
from app.services.watn_pipeline.normalize import clean_str, parse_amount, parse_date

logger = logging.getLogger(__name__)

BULK_URLS = (
    "https://data.www.sbir.gov/mod_awarddatapublic/award_data.csv",
    "https://data.www.sbir.gov/awarddatapublic/award_data.csv",
)
USECOLS = [
    "Company", "Award Title", "Agency", "Branch", "Phase", "Program", "Agency Tracking Number",
    "Contract", "Proposal Award Date", "Contract End Date", "Solicitation Number", "Topic Code",
    "Award Year", "Award Amount", "UEI", "Duns", "Number Employees", "Company Website",
    "Address1", "Address2", "City", "State", "Zip", "HUBZone Owned",
    "Socially and Economically Disadvantaged", "Woman Owned", "PI Name", "Contact Name",
]


class SbirIndex:
    def __init__(self, cache_dir: Path, max_age_days: int = 7, csv_path: Optional[Path] = None):
        self.cache_dir = Path(cache_dir)
        self.max_age_days = max_age_days
        self.csv_path = Path(csv_path) if csv_path else None
        self._lock = threading.Lock()
        self._loaded = False
        self.error: Optional[str] = None
        self.by_uei: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.by_duns: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.by_token: Dict[str, set] = defaultdict(set)  # name token -> normalised names (fuzzy blocking)

    # ── Loading ────────────────────────────────────────────────────────

    def ensure_loaded(self) -> bool:
        with self._lock:
            if not self._loaded:
                try:
                    self._load(self._local_csv())
                except Exception as exc:
                    logger.exception("SBIR bulk data could not be loaded")
                    self.error = str(exc)
                self._loaded = True
        return self.error is None

    def _local_csv(self) -> Path:
        if self.csv_path:
            return self.csv_path
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / "sbir_award_data.csv"
        fresh = target.exists() and (time.time() - target.stat().st_mtime) < self.max_age_days * 86400
        if fresh:
            return target
        last_exc: Optional[Exception] = None
        for url in BULK_URLS:
            try:
                logger.info("Downloading SBIR bulk award data from %s", url)
                tmp = target.with_suffix(".part")
                with http.request("GET", url, stream=True, timeout=600) as resp, open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                os.replace(tmp, target)
                return target
            except Exception as exc:
                last_exc = exc
                logger.warning("SBIR bulk download failed from %s: %s", url, exc)
        if target.exists():
            logger.warning("Using stale SBIR bulk file %s", target)
            return target
        raise RuntimeError(f"SBIR bulk download failed: {last_exc}")

    def _load(self, path: Path) -> None:
        import pandas as pd

        df = pd.read_csv(
            path,
            usecols=lambda c: c.lstrip("﻿") in USECOLS,
            dtype=str,
            encoding="utf-8-sig",
            on_bad_lines="skip",
        )
        df.columns = [c.lstrip("﻿") for c in df.columns]
        for raw in df.to_dict("records"):
            row = {k: clean_str(v) for k, v in raw.items()}  # NaN/blank -> None
            uei = normalize_id(row.get("UEI"))
            duns = normalize_duns(row.get("Duns"))
            name = normalize_name(row.get("Company"))
            if uei:
                self.by_uei[uei].append(row)
            if duns:
                self.by_duns[duns].append(row)
            if name:
                self.by_name[name].append(row)
                for token in name.split()[:2]:
                    if len(token) >= 3:
                        self.by_token[token].add(name)
        logger.info("SBIR index loaded: %d awards, %d UEIs", len(df), len(self.by_uei))

    # ── Lookups ────────────────────────────────────────────────────────

    def uei_for_duns(self, duns: Optional[str]) -> Optional[str]:
        for row in self.by_duns.get(normalize_duns(duns) or "", []):
            uei = normalize_id(row.get("UEI"))
            if uei:
                return uei
        return None

    def firms_named(self, name: str, min_similarity: float = 70.0, limit: int = 10) -> List[Dict[str, Any]]:
        """
        One representative row per distinct SBIR firm whose name resembles ``name``.

        Candidates share one of the input's first two name tokens; final scoring
        (with website/state/contacts) happens in the identity resolver.
        """
        target = normalize_name(name)
        if not target:
            return []
        pool = {target} if target in self.by_name else set()
        for token in target.split()[:2]:
            pool |= self.by_token.get(token, set())
        ranked = sorted(((name_similarity(target, n), n) for n in pool), reverse=True)
        firms: Dict[str, Dict[str, Any]] = {}
        for score, norm in ranked[: limit * 3]:
            if score < min_similarity:
                break
            for row in self.by_name.get(norm, []):
                key = normalize_id(row.get("UEI")) or normalize_duns(row.get("Duns")) or row.get("Company")
                firms.setdefault(key, row)
        return list(firms.values())[:limit]

    def awards(self, uei: Optional[str], duns: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = list(self.by_uei.get(normalize_id(uei) or "", []))
        seen = {id(r) for r in rows}
        # Older awards carry only DUNS; include them when DUNS is known.
        for row in self.by_duns.get(normalize_duns(duns) or "", []):
            if id(row) not in seen:
                rows.append(row)
        return rows


def _phase(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return raw.replace("Phase", "").strip() or None


def to_funding(row: Dict[str, Any]) -> FundingRecord:
    branch = row.get("Branch")
    agency = row.get("Agency")
    program = (row.get("Program") or "SBIR").strip().upper()
    info = "; ".join(
        f"{label}: {row[key]}"
        for label, key in (("Agency", "Agency"), ("Branch", "Branch"), ("Topic", "Topic Code"),
                           ("Solicitation", "Solicitation Number"), ("PI", "PI Name"))
        if row.get(key)
    )
    awarded = parse_date(row.get("Proposal Award Date"))
    reasons = []
    if awarded is None:
        awarded = _year_start(row.get("Award Year"))
        if awarded:
            reasons.append("award date estimated from award year")
    return FundingRecord(
        source="sbir",
        review_reasons=reasons,
        amount=parse_amount(row.get("Award Amount")),
        date_awarded=awarded,
        end_date=parse_date(row.get("Contract End Date")),
        award_number=row.get("Contract") or row.get("Agency Tracking Number"),
        external_id=row.get("Agency Tracking Number"),
        title=row.get("Award Title"),
        investor=branch or agency,
        phase=_phase(row.get("Phase")),
        program=program,
        raw_type=program,
        funding_source="STTR" if program == "STTR" else "SBIR",
        source_url="https://www.sbir.gov/awards",
        additional_info=info or None,
        recipient_name=row.get("Company"),
        recipient_uei=normalize_id(row.get("UEI")),
    )


def firm_profile(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Company attributes from the most recent SBIR award row."""
    if not rows:
        return {}
    latest = max(rows, key=lambda r: (r.get("Proposal Award Date") or r.get("Award Year") or ""))
    employees = parse_amount(latest.get("Number Employees"))
    return {
        "legal_name": latest.get("Company"),
        "website": latest.get("Company Website"),
        "address1": latest.get("Address1"),
        "address2": latest.get("Address2"),
        "city": latest.get("City"),
        "state": latest.get("State"),
        "zip": latest.get("Zip"),
        "employees": int(employees) if employees else None,
        "woman_owned": _yn(latest.get("Woman Owned")),
        "hubzone_owned": _yn(latest.get("HUBZone Owned")),
        "disadvantaged": _yn(latest.get("Socially and Economically Disadvantaged")),
        "uei": normalize_id(latest.get("UEI")),
        "duns": normalize_duns(latest.get("Duns")),
    }


def _yn(value: Optional[str]) -> Optional[bool]:
    if not value:
        return None
    return value.strip().upper().startswith("Y")


def _year_start(year: Optional[str]) -> Optional[date]:
    try:
        return date(int(float(year)), 1, 1) if year else None
    except ValueError:
        return None
