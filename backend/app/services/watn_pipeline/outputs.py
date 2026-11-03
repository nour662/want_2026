"""
Output writers.

finalized_results/
    company_info.csv          one row per participant company
    funding_info.csv          publishable funding records (Salesforce-ready columns)
    new_funding_records.csv   records not already in the baseline export (when given)
    review_queue.csv          funding records and identity matches a person should check
run_report.json               coverage, match quality and merge statistics

Column names follow the existing WATN/Salesforce exports so the files can be
diffed against previous runs and loaded with the same mappings. Writing to
Salesforce later means adding a sink that consumes the same rows.
"""

from __future__ import annotations

import calendar
import csv
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.watn_pipeline.evaluate import evaluate_funding, is_publishable
from app.services.watn_pipeline.matching import normalize_id, normalize_name
from app.services.watn_pipeline.merge import award_key, awards_equal
from app.services.watn_pipeline.models import CompanyResult, FundingRecord
from app.services.watn_pipeline.normalize import clean_str, is_blank, parse_amount, parse_date

FUNDING_COLUMNS = [
    "Funding: Funding ID", "Record Status", "Account Name", "I-Corps Team: CorpID", "UEI",
    "Date Awarded", "Amount Awarded", "Award End Month", "Award End Year", "Funding Source",
    "Funding Stage", "Investors", "Phase", "Award Title", "Award Number", "Source Website",
    "Original Source", "Contract Award Type", "Additional Info", "Merged",
    "Received Post I-Corps Participation", "Match Confidence", "Match Method", "Sources", "Review Flags",
]

COMPANY_COLUMNS = [
    "Account Name", "I-Corps Team: CorpID", "Entrepreneur: EID", "Earliest Regional Cohort Date",
    "Legal Name", "UEI", "DUNS", "CAGE", "Company Exists", "Active", "Company Website", "Employees",
    "Street Address", "City", "State", "Zip Code", "Country", "Legislative District", "NAICS",
    "SAM Registration Date", "Incorporation State", "Incorporation Country", "Incorporation Type",
    "Woman Owned", "HUBZone Owned", "Socially and Economically Disadvantaged", "University Affiliation",
    "Funding Records", "Total Funding", "Identity Match Score", "Identity Match Tier",
    "Identity Match Method", "Identity Match Reasons", "Field Sources", "Source Status", "Review Flags",
]

REVIEW_COLUMNS = ["Review Type", "Account Name", "UEI", "Detail"] + FUNDING_COLUMNS[5:]

ORIGINAL_SOURCE = {
    "sbir": "SBIR", "usas_contracts": "USASpending Contracts", "usas_grants": "USASpending Grants",
    "nsf": "NSF", "tedco": "TEDCO",
}


def write_all(results: List[CompanyResult], out: Path, config: Any, unavailable: Dict[str, str]) -> Dict[str, Any]:
    final = out / "finalized_results"
    final.mkdir(parents=True, exist_ok=True)

    for res in results:  # cohort dates can change between resumed runs
        for rec in res.funding:
            evaluate_funding(rec, res.company.cohort_date)

    baseline = _load_baseline(config.baseline_funding) if config.baseline_funding else None
    next_id = _next_funding_id(baseline) if baseline else None

    funding_rows, new_rows, review_rows, company_rows = [], [], [], []
    for res in results:
        uei = _profile(res, "uei")
        published = [r for r in res.funding if is_publishable(r)]
        for rec in res.funding:
            row = _funding_row(res, rec, uei)
            if not is_publishable(rec):
                review_type = "tedco mention" if rec.source == "tedco" and rec.amount is None else "funding"
                review_rows.append({"Review Type": review_type, "Account Name": res.company.name, "UEI": uei,
                                    "Detail": row["Review Flags"], **{k: row[k] for k in FUNDING_COLUMNS[5:]}})
                continue
            if baseline is not None:
                match = _match_baseline(baseline.get(normalize_name(res.company.name), []), rec)
                if match:
                    row["Funding: Funding ID"], row["Record Status"] = match, "Existing Database Record"
                else:
                    row["Funding: Funding ID"], row["Record Status"] = f"FID-{next_id:04d}", "New Record"
                    next_id += 1
                    new_rows.append(row)
            else:
                row["Record Status"] = "Scraped Record"
            funding_rows.append(row)
        for reason in res.review_reasons:
            review_rows.append({"Review Type": "company", "Account Name": res.company.name, "UEI": uei, "Detail": reason})
        if res.status != "completed":
            review_rows.append({"Review Type": "error" if res.status == "failed" else "partial", "Account Name": res.company.name, "UEI": uei,
                                "Detail": res.error or res.status})
        company_rows.append(_company_row(res, published))

    _write_csv(final / "funding_info.csv", FUNDING_COLUMNS, funding_rows)
    _write_csv(final / "company_info.csv", COMPANY_COLUMNS, company_rows)
    _write_csv(final / "review_queue.csv", REVIEW_COLUMNS, review_rows)
    if baseline is not None:
        _write_csv(final / "new_funding_records.csv", FUNDING_COLUMNS, new_rows)

    report = _report(results, funding_rows, new_rows if baseline is not None else None, review_rows, unavailable)
    (out / "run_report.json").write_text(json.dumps(report, indent=2, default=str))
    return report


# ── Rows ───────────────────────────────────────────────────────────────


def _profile(res: CompanyResult, name: str) -> Any:
    fv = res.profile.get(name)
    return None if fv is None or is_blank(fv.value) else fv.value


def _funding_row(res: CompanyResult, rec: FundingRecord, uei: Optional[str]) -> Dict[str, Any]:
    end = rec.end_date
    return {
        "Funding: Funding ID": "",
        "Record Status": "",
        "Account Name": res.company.name,
        "I-Corps Team: CorpID": "; ".join(res.company.corp_ids),
        "UEI": uei or "",
        "Date Awarded": rec.date_awarded.isoformat() if rec.date_awarded else "",
        "Amount Awarded": f"{rec.amount:.2f}" if rec.amount is not None else "",
        "Award End Month": calendar.month_name[end.month] if end else "",
        "Award End Year": end.year if end else "",
        "Funding Source": rec.funding_source or "",
        "Funding Stage": rec.funding_stage or "",
        "Investors": rec.investor or "",
        "Phase": rec.phase or "",
        "Award Title": rec.title or "",
        "Award Number": rec.award_number or "",
        "Source Website": rec.source_url or "",
        "Original Source": ORIGINAL_SOURCE.get(rec.source, rec.source),
        "Contract Award Type": rec.raw_type if rec.source.startswith("usas") else "",
        "Additional Info": rec.additional_info or "",
        "Merged": rec.merged_count > 1,
        "Received Post I-Corps Participation": "" if rec.post_icorps is None else rec.post_icorps,
        "Match Confidence": f"{rec.confidence:.0f}",
        "Match Method": rec.match_method,
        "Sources": "; ".join(ORIGINAL_SOURCE.get(s, s) for s in (rec.sources or [rec.source])),
        "Review Flags": "; ".join(rec.review_reasons),
    }


def _incorporation_type(legal_name: Optional[str]) -> str:
    name = (legal_name or "").lower()
    if re.search(r"\bl\.?l\.?c\b", name):
        return "LLC"
    if re.search(r"\b(inc|incorporated|corp|corporation)\b", name):
        return "Corporation"
    return ""


def _company_row(res: CompanyResult, published: List[FundingRecord]) -> Dict[str, Any]:
    c, p, ident = res.company, res.profile, res.identity
    legal = _profile(res, "legal_name")
    exists = any(s.startswith("ok") for s in res.source_status.values()) or bool(published)
    return {
        "Account Name": c.name,
        "I-Corps Team: CorpID": "; ".join(c.corp_ids),
        "Entrepreneur: EID": "; ".join(c.eids),
        "Earliest Regional Cohort Date": c.cohort_date.isoformat() if c.cohort_date else "",
        "Legal Name": legal or "",
        "UEI": _profile(res, "uei") or "",
        "DUNS": _profile(res, "duns") or "",
        "CAGE": _profile(res, "cage") or "",
        "Company Exists": exists,
        "Active": "" if _profile(res, "active") is None else _profile(res, "active"),
        "Company Website": _profile(res, "website") or "",
        "Employees": _profile(res, "employees") or "",
        "Street Address": " ".join(filter(None, [_profile(res, "address1"), _profile(res, "address2")])),
        "City": (_profile(res, "city") or "").title(),
        "State": _profile(res, "state") or "",
        "Zip Code": _profile(res, "zip") or "",
        "Country": _profile(res, "country") or "",
        "Legislative District": _profile(res, "congressional_district") or "",
        "NAICS": _profile(res, "naics") or "",
        "SAM Registration Date": _profile(res, "registration_date") or "",
        "Incorporation State": _profile(res, "incorporation_state") or "",
        "Incorporation Country": _profile(res, "incorporation_country") or "",
        "Incorporation Type": _incorporation_type(legal),
        "Woman Owned": _fmt_bool(_profile(res, "woman_owned")),
        "HUBZone Owned": _fmt_bool(_profile(res, "hubzone_owned")),
        "Socially and Economically Disadvantaged": _fmt_bool(_profile(res, "disadvantaged")),
        "University Affiliation": _profile(res, "university") or "",
        "Funding Records": len(published),
        "Total Funding": f"{sum(r.amount or 0 for r in published):.2f}",
        "Identity Match Score": f"{ident.score:.0f}" if ident else "",
        "Identity Match Tier": ident.tier if ident else "",
        "Identity Match Method": ident.method if ident else "",
        "Identity Match Reasons": "; ".join(ident.reasons) if ident else "",
        "Field Sources": "; ".join(f"{k}={v.source}" for k, v in sorted(p.items())),
        "Source Status": "; ".join(f"{k}={v}" for k, v in sorted(res.source_status.items())),
        "Review Flags": "; ".join(res.review_reasons),
    }


def _fmt_bool(value: Optional[bool]) -> Any:
    return "" if value is None else value


def _write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


# ── Baseline (existing Salesforce records) ─────────────────────────────


def _load_baseline(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    by_company: Dict[str, List[Dict[str, Any]]] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            fid = clean_str(row.get("Funding: Funding ID"))
            name = clean_str(row.get("Account Name"))
            if not fid or not name:
                continue
            url = clean_str(row.get("Source Website")) or ""
            by_company.setdefault(normalize_name(name), []).append({
                "id": fid,
                "amount": parse_amount(row.get("Amount Awarded")),
                "date": parse_date(row.get("Date Awarded")),
                "source": clean_str(row.get("Funding Source")),
                "award": award_key(row.get("Award Number")),
                "url": url,
                "url_key": normalize_id(url) or "",
                "used": False,
            })
    return by_company


def _next_funding_id(baseline: Dict[str, List[Dict[str, Any]]]) -> int:
    numbers = [int(m.group(1)) for rows in baseline.values() for r in rows if (m := re.search(r"(\d+)$", r["id"]))]
    return max(numbers, default=0) + 1


def _match_baseline(rows: List[Dict[str, Any]], rec: FundingRecord) -> Optional[str]:
    key = award_key(rec.award_number)
    ext = normalize_id(rec.external_id) or ""

    def amount_close(a: Optional[float], b: Optional[float], tol: float) -> bool:
        return a is not None and b is not None and abs(a - b) <= max(1.0, tol * max(abs(a), abs(b)))

    tests = (
        lambda r: bool(rec.source_url) and r["url"] == rec.source_url,
        lambda r: bool(key) and (awards_equal(r["award"], key) or (len(key) >= 8 and key in r["url_key"])),
        lambda r: bool(ext) and len(ext) >= 8 and ext in r["url_key"],
        lambda r: rec.date_awarded is not None and r["date"] is not None
        and abs((r["date"] - rec.date_awarded).days) <= 45 and amount_close(r["amount"], rec.amount, 0.02),
        lambda r: rec.date_awarded is not None and r["date"] is not None and r["date"].year == rec.date_awarded.year
        and r["source"] == rec.funding_source and amount_close(r["amount"], rec.amount, 0.005),
        lambda r: rec.date_awarded is not None and r["date"] is not None and r["date"].year == rec.date_awarded.year
        and amount_close(r["amount"], rec.amount, 0.001),
    )
    for test in tests:  # strongest evidence first; each baseline row is claimed once
        for r in rows:
            if not r["used"] and test(r):
                r["used"] = True
                return r["id"]
    return None


# ── Report ─────────────────────────────────────────────────────────────


def _report(results, funding_rows, new_rows, review_rows, unavailable) -> Dict[str, Any]:
    tiers = Counter((r.identity.tier if r.identity else "none") for r in results)
    methods = Counter((r.identity.method.split(":")[-1].split("+")[0] if r.identity else "none") for r in results)
    source_status: Dict[str, Counter] = {}
    for r in results:
        for name, status in r.source_status.items():
            source_status.setdefault(name, Counter())[status.split(":")[0]] += 1
    return {
        "generated": date.today().isoformat(),
        "companies": len(results),
        "companies_failed": sum(r.status == "failed" for r in results),
        "companies_partial": sum(r.status == "partial" for r in results),
        "companies_with_uei": sum(bool(r.resolved_uei) for r in results),
        "identity_tiers": dict(tiers),
        "identity_methods": dict(methods),
        "unavailable_sources": unavailable,
        "source_status": {k: dict(v) for k, v in source_status.items()},
        "funding_published": len(funding_rows),
        "funding_new_vs_baseline": len(new_rows) if new_rows is not None else None,
        "funding_by_source": dict(Counter(r["Funding Source"] for r in funding_rows)),
        "funding_by_original_source": dict(Counter(r["Original Source"] for r in funding_rows)),
        "funding_merged_records": sum(1 for r in funding_rows if r["Merged"]),
        "duplicates_collapsed": sum(r.duplicates_merged for r in results),
        "review_items": len(review_rows),
        "review_by_type": dict(Counter(r["Review Type"] for r in review_rows)),
    }
