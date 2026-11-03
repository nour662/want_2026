"""
Participant input loading.

Accepts the existing WATN participant CSVs (one row per entrepreneur) and
collapses them to one ``CompanyInput`` per company. Column names vary between
exports, so each field is read from the first alias present. A Salesforce
loader can produce the same ``CompanyInput`` objects later.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from app.services.watn_pipeline.matching import normalize_duns, normalize_id, normalize_name
from app.services.watn_pipeline.models import CompanyInput
from app.services.watn_pipeline.normalize import clean_str, parse_date

ALIASES = {
    "company": ("Company", "Company Name", "Account Name"),
    "uei": ("UEI",),
    "duns": ("DUNS", "Duns"),
    "website": ("Website", "Company Website"),
    "city": ("City",),
    "state": ("State",),
    "university": ("University Affiliation",),
    "cohort_date": ("Regional Start Date", "Earliest Regional Cohort Date", "Cohort Date"),
    "corp_id": ("CorpID", "I-Corps Team: CorpID"),
    "eid": ("EID", "Entrepreneur: EID"),
    "contact": ("Name", "Entrepreneur Name", "Full Name"),
    "external_id": ("Account ID", "Salesforce Id"),
}


def _get(row: Dict[str, str], field: str) -> Optional[str]:
    for alias in ALIASES[field]:
        if alias in row:
            value = clean_str(row.get(alias))
            if value:
                return value
    return None


def company_key(name: str) -> str:
    return normalize_name(name) or name.strip().lower()


def load_participants(paths: Iterable[Path]) -> List[CompanyInput]:
    companies: Dict[str, CompanyInput] = {}
    for path in paths:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                row = {(k or "").strip(): v for k, v in row.items()}
                name = _get(row, "company")
                if not name:
                    continue
                key = company_key(name)
                company = companies.get(key)
                if company is None:
                    company = companies[key] = CompanyInput(key=key, name=name)
                _absorb(company, row)
    return list(companies.values())


def _absorb(company: CompanyInput, row: Dict[str, str]) -> None:
    company.uei = company.uei or normalize_id(_get(row, "uei"))
    company.duns = company.duns or normalize_duns(_get(row, "duns"))
    company.website = company.website or _get(row, "website")
    company.city = company.city or _get(row, "city")
    company.state = company.state or _get(row, "state")
    company.external_id = company.external_id or _get(row, "external_id")
    university = _get(row, "university")
    if university and university.lower() != "other":
        company.university = company.university or university

    cohort = parse_date(_get(row, "cohort_date"))
    if cohort and (company.cohort_date is None or cohort < company.cohort_date):
        company.cohort_date = cohort

    contact = _get(row, "contact")
    if not contact:
        first, last = clean_str(row.get("First Name")), clean_str(row.get("Last Name"))
        contact = " ".join(p for p in (first, last) if p) or None
    for value, bucket in ((contact, company.contacts), (_get(row, "corp_id"), company.corp_ids), (_get(row, "eid"), company.eids)):
        if value:
            for part in value.split(";"):
                part = part.strip()
                if part and part not in bucket:
                    bucket.append(part)
