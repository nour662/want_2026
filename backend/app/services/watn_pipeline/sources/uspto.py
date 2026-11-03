"""
USPTO patents owned by the company, from the Open Data Portal (api.uspto.gov).

Only the company is searched: applications where it is the applicant or a
recorded assignee. Inventor names are never searched; they are only used as
corroboration when a participant turns out to be an inventor on a patent the
company owns.

Every application is scored with ``score_entity`` against the company name
(and its registered SAM/SBIR names), so "Hale" does not collect every patent
of "Hale Products Inc". Needs ``USPTO_API_KEY`` (free, from data.uspto.gov).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from app.services.watn_pipeline import http
from app.services.watn_pipeline.matching import _GENERIC_WORDS, REVIEW, normalize_name, score_entity
from app.services.watn_pipeline.models import CompanyInput, MatchEvidence, PatentRecord
from app.services.watn_pipeline.normalize import clean_str, parse_date

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
APPLICANT_FIELD = "applicationMetaData.applicantBag.applicantNameText"
ASSIGNEE_FIELD = "assignmentBag.assigneeBag.assigneeNameText"
PAGE_SIZE = 100
MAX_RESULTS = 300  # per name; a startup with more hits than this has a name that is too broad


class UsptoPatents:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("USPTO_API_KEY") or None
        self.error: Optional[str] = None if self.api_key else "USPTO_API_KEY not set"
        self._fields: Tuple[str, ...] = (APPLICANT_FIELD, ASSIGNEE_FIELD)
        self._combined = True  # one OR query per name; falls back to per-field queries on HTTP 400

    @property
    def available(self) -> bool:
        return self.error is None

    def check(self) -> bool:
        """One small query so a bad key is reported once, not as an error on every company."""
        if not self.available:
            return False
        try:
            self._post({"q": f'{APPLICANT_FIELD}:"university"', "pagination": {"offset": 0, "limit": 1}})
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code in (401, 403):
                self.error = f"USPTO API key rejected (HTTP {code})"
                return False
            if code != 404:
                raise
        return True

    # ── Search ──────────────────────────────────────────────────────

    def find(self, company: CompanyInput, registered_names: Iterable[str] = ()) -> List[PatentRecord]:
        names = _query_names([company.name, *registered_names])
        found: Dict[str, PatentRecord] = {}
        for name in names:
            for item in self._search_name(name):
                record = _to_patent(item, company, names)
                if record is None:
                    continue
                prev = found.get(record.application_number)
                if prev is None or record.confidence > prev.confidence:
                    found[record.application_number] = record
        return sorted(found.values(), key=lambda p: p.filing_date or date.min)

    def _search_name(self, name: str) -> List[Dict[str, Any]]:
        phrase = f'"{name}"'
        if self._combined:
            try:
                return self._paged(" OR ".join(f"{f}:{phrase}" for f in self._fields))
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 400:
                    raise
                logger.warning("USPTO rejected the combined applicant/assignee query; using one query per field")
                self._combined = False
        items: List[Dict[str, Any]] = []
        for f in self._fields:
            try:
                items.extend(self._paged(f"{f}:{phrase}"))
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 400 or f == APPLICANT_FIELD:
                    raise
                logger.warning("USPTO does not accept %s; searching applicants only", f)
                self._fields = (APPLICANT_FIELD,)
        return items

    def _paged(self, query: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        while offset < MAX_RESULTS:
            try:
                data = self._post({"q": query, "pagination": {"offset": offset, "limit": PAGE_SIZE}})
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:  # ODP's "no matching records"
                    break
                raise
            page = data.get("patentFileWrapperDataBag") or []
            items.extend(page)
            offset += PAGE_SIZE
            if len(page) < PAGE_SIZE or offset >= int(data.get("count") or 0):
                break
        return items

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return http.post_json(SEARCH_URL, payload, headers={"X-API-KEY": self.api_key}, timeout=30) or {}


# ── Parsing and scoring ────────────────────────────────────────────────


def _query_names(names: Iterable[Optional[str]]) -> List[str]:
    out: List[str] = []
    for name in names:
        norm = normalize_name(name)
        if len(norm) < 3 or not [t for t in norm.split() if t not in _GENERIC_WORDS]:
            continue  # "Technologies Inc" alone would match thousands of owners
        if norm not in out:
            out.append(norm)
    return out


def _state(address_bag: Any) -> Optional[str]:
    bags = address_bag if isinstance(address_bag, list) else [address_bag]
    for addr in bags:
        if isinstance(addr, dict) and clean_str(addr.get("geographicRegionCode")):
            return clean_str(addr.get("geographicRegionCode"))
    return None


def _owners(item: Dict[str, Any]) -> List[Tuple[str, str, Optional[str]]]:
    """(name, role, state) for every applicant and recorded assignee."""
    meta = item.get("applicationMetaData") or {}
    owners: List[Tuple[str, str, Optional[str]]] = []
    for applicant in meta.get("applicantBag") or []:
        name = clean_str(applicant.get("applicantNameText"))
        if name:
            owners.append((name, "applicant", _state(applicant.get("correspondenceAddressBag"))))
    first = clean_str(meta.get("firstApplicantName"))
    if first and not any(o[0] == first for o in owners):
        owners.append((first, "applicant", None))
    for assignment in item.get("assignmentBag") or []:
        for assignee in assignment.get("assigneeBag") or []:
            name = clean_str(assignee.get("assigneeNameText"))
            if name and not any(o[0] == name and o[1] == "assignee" for o in owners):
                owners.append((name, "assignee", _state(assignee.get("assigneeAddress"))))
    return owners


def _inventors(meta: Dict[str, Any]) -> List[str]:
    names = []
    for inv in meta.get("inventorBag") or []:
        name = clean_str(inv.get("inventorNameText")) or " ".join(
            p for p in (clean_str(inv.get("firstName")), clean_str(inv.get("lastName"))) if p)
        if name and name not in names:
            names.append(name)
    return names


def _cpc(meta: Dict[str, Any]) -> List[str]:
    codes = []
    for code in meta.get("cpcClassificationBag") or []:
        text = clean_str(code if isinstance(code, str) else (code or {}).get("cpcClassificationText"))
        if text:
            codes.append(" ".join(text.split()))
    return codes


def _to_patent(item: Dict[str, Any], company: CompanyInput, query_names: List[str]) -> Optional[PatentRecord]:
    app_no = clean_str(item.get("applicationNumberText"))
    meta = item.get("applicationMetaData") or {}
    if not app_no:
        return None
    inventors = _inventors(meta)

    best: Optional[MatchEvidence] = None
    best_owner: Optional[Tuple[str, str, Optional[str]]] = None
    for owner in _owners(item):
        for input_name in [company.name, *query_names]:
            ev = score_entity(input_name=input_name, candidate_names=[owner[0]], input_state=company.state,
                              candidate_state=owner[2], input_contacts=company.contacts, candidate_contacts=inventors)
            if best is None or ev.score > best.score:
                best, best_owner = ev, owner
    if best is None or best.score < REVIEW:
        return None

    patent_no = clean_str(meta.get("patentNumber"))
    return PatentRecord(
        application_number=app_no,
        title=clean_str(meta.get("inventionTitle")),
        patent_number=patent_no,
        publication_number=clean_str(meta.get("earliestPublicationNumber")),
        status=clean_str(meta.get("applicationStatusDescriptionText")),
        application_type=clean_str(meta.get("applicationTypeLabelName")),
        filing_date=parse_date(meta.get("filingDate")),
        publication_date=parse_date(meta.get("earliestPublicationDate")),
        grant_date=parse_date(meta.get("grantDate")),
        owner=best_owner[0],
        owner_role=best_owner[1],
        owner_state=best_owner[2],
        inventors=inventors,
        cpc_codes=_cpc(meta),
        source_url=f"https://patentcenter.uspto.gov/applications/{app_no}",
        confidence=best.score,
        tier=best.tier,
        match_method=f"{best_owner[1]}:{best.method}",
        match_reasons=best.reasons,
    )
