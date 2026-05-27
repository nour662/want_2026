import html
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urljoin

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.models.company import Company
from app.models.funding import FundingRecord

router = APIRouter(prefix="/integrations", tags=["integrations"])

TEDCO_BASE_URL = "https://www.tedcomd.com"
TEDCO_PRESS_RELEASES_URL = f"{TEDCO_BASE_URL}/news-events/press-releases?page={{page}}"
TEDCO_HEADERS = {
    "User-Agent": "want_FullStack TEDCO Sync/1.0 (+https://www.tedcomd.com)",
    "Accept-Language": "en-US,en;q=0.9",
}
STRICT_MATCH_THRESHOLD = 0.92
POSITIVE_TITLE_MARKERS = (
    "invests in",
    "investment in",
    "supports",
    "matching funds",
    "awardees",
    "recent investment",
    "announces recent investment",
)
NEGATIVE_TITLE_MARKERS = (
    "meeting notice",
    "board of directors",
    "committee meeting",
    "entrepreneur expo",
    "keynote",
    "discussion",
    "honorees",
    "selected as",
    "host",
    "cohort at",
)
COMPANY_SUFFIXES = {
    "inc",
    "inc.",
    "llc",
    "l.l.c",
    "ltd",
    "corp",
    "corporation",
    "company",
    "co",
    "pllc",
    "lp",
    "llp",
}


class IntegrationStubResponse(BaseModel):
    status: str
    detail: str
    uei: Optional[str] = None
    duns: Optional[str] = None
    name: Optional[str] = None


class TedcoFundingMatchItem(BaseModel):
    release_title: str
    release_url: str
    release_date: Optional[str] = None
    extracted_company: str
    matched_company_id: Optional[str] = None
    matched_company_name: Optional[str] = None
    amount_awarded: Optional[float] = None
    program_name: Optional[str] = None
    match_score: float = 0.0
    decision: str
    reason: Optional[str] = None


class TedcoFundingSyncResponse(BaseModel):
    success: bool
    pages_scanned: int
    releases_scanned: int
    funding_candidates: int
    matched_companies: int
    inserted_records: int
    skipped_records: int
    strict_mode: bool = True
    items: list[TedcoFundingMatchItem] = Field(default_factory=list)


def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(value or "").lower()).strip()


def _normalize_company_name(name: Optional[str]) -> str:
    tokens = _normalize_text(name).split()
    while tokens and tokens[-1] in COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _similarity(left: Optional[str], right: Optional[str]) -> float:
    normalized_left = _normalize_company_name(left)
    normalized_right = _normalize_company_name(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _fetch_html(url: str) -> str:
    response = requests.get(url, headers=TEDCO_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def _clean_html_to_text(raw_html: str) -> str:
    start_match = re.search(r"<h1[^>]*>.*?</h1>", raw_html, re.IGNORECASE | re.DOTALL)
    if start_match:
        raw_html = raw_html[start_match.start():]

    end_positions = []
    for pattern in [
        r"<h[23][^>]*>\s*About TEDCO\s*</h[23]>",
        r"<h[23][^>]*>\s*Related Funds\s*</h[23]>",
        r"<h[23][^>]*>\s*Recent Press Releases\s*</h[23]>",
    ]:
        match = re.search(pattern, raw_html, re.IGNORECASE | re.DOTALL)
        if match:
            end_positions.append(match.start())
    if end_positions:
        raw_html = raw_html[: min(end_positions)]

    cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", raw_html)
    cleaned = re.sub(r"(?i)</(p|div|section|article|li|h1|h2|h3|h4)>", "\n", cleaned)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned).replace("\xa0", " ")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    return cleaned.strip()


def _extract_listing_links(raw_html: str) -> tuple[list[str], int]:
    links = []
    seen = set()
    for href in re.findall(r'href="(/news-events/press-releases/\d{4}/[^"#?]+)"', raw_html, re.IGNORECASE):
        absolute_url = urljoin(TEDCO_BASE_URL, href)
        if absolute_url not in seen:
            seen.add(absolute_url)
            links.append(absolute_url)

    page_numbers = [int(value) for value in re.findall(r'press-releases\?page=(\d+)', raw_html, re.IGNORECASE)]
    last_page = max(page_numbers) if page_numbers else 0
    return links, last_page


def _extract_release_date(text: str) -> Optional[datetime]:
    match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s+\d{4}\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0).title(), "%B %d, %Y")
    except ValueError:
        return None


def _parse_amount(raw_amount: Optional[str]) -> Optional[Decimal]:
    if not raw_amount:
        return None
    try:
        return Decimal(re.sub(r"[^\d.]", "", raw_amount))
    except (InvalidOperation, ValueError):
        return None


def _extract_company_from_title(title: str) -> Optional[str]:
    patterns = [
        r"TEDCO(?:’s|'s)?(?:\s+.+?)?\s+(?:Invests in|Announces Recent Investment in|Supports)\s+(?P<company>.+)$",
        r"(?:Invests in|Supports)\s+(?P<company>.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            company = match.group("company").strip(" .,:;–-")
            return company or None
    return None


def _extract_program_name(title: str, text: str) -> Optional[str]:
    title_patterns = [
        r"TEDCO(?:’s|'s)?\s+(?P<program>.+?)\s+(?:Invests in|Supports)\s+",
        r"Awards a Total of \$[\d,]+ in (?P<program>.+?) to",
    ]
    for pattern in title_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return match.group("program").strip(" .,:;–-") or None

    body_patterns = [
        r"\$[\d,]+\s+(?P<program>[A-Z][A-Za-z/&\-\s]+?)\s+(?:investment|funding)\s+in",
        r"as part of the\s+(?P<program>[A-Z][A-Za-z/&\-\s]+?)\s+program",
    ]
    for pattern in body_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group("program").strip(" .,:;–-") or None

    return None


def _is_funding_release(title: str, text: str) -> bool:
    normalized_title = title.lower()
    if any(marker in normalized_title for marker in NEGATIVE_TITLE_MARKERS):
        return False
    if any(marker in normalized_title for marker in POSITIVE_TITLE_MARKERS):
        return True
    return "matching funds" in text.lower() and "$" in text


def _extract_release_candidates(title: str, text: str, url: str) -> list[dict]:
    if not _is_funding_release(title, text):
        return []

    release_date = _extract_release_date(text)
    program_name = _extract_program_name(title, text)
    results: list[dict] = []

    multi_awardee_matches = list(
        re.finditer(
            r"\d+\.\s*(?P<company>[A-Z0-9][A-Za-z0-9&.,'’\- ]{1,120}?)\s*,\s*located.*?for\s+\$(?P<amount>[\d,]+(?:\.\d+)?)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
    )
    if multi_awardee_matches:
        for match in multi_awardee_matches:
            amount_awarded = _parse_amount(match.group("amount"))
            company_name = match.group("company").strip(" .,:;–-")
            if company_name and amount_awarded is not None:
                results.append(
                    {
                        "release_title": title,
                        "release_url": url,
                        "release_date": release_date,
                        "company_name": company_name,
                        "amount_awarded": amount_awarded,
                        "program_name": program_name,
                        "reason": "multi_awardee_list",
                    }
                )
        return results

    company_name = _extract_company_from_title(title)
    amount_match = re.search(r"\$[\d,]+(?:\.\d+)?", text)
    amount_awarded = _parse_amount(amount_match.group(0) if amount_match else None)

    if company_name and amount_awarded is not None:
        results.append(
            {
                "release_title": title,
                "release_url": url,
                "release_date": release_date,
                "company_name": company_name,
                "amount_awarded": amount_awarded,
                "program_name": program_name,
                "reason": "single_company_release",
            }
        )

    return results


def _match_company_in_database(company_name: str, db: Session) -> tuple[Optional[Company], float]:
    normalized_name = _normalize_company_name(company_name)
    if len(normalized_name) < 3:
        return None, 0.0

    tokens = normalized_name.split()
    search_terms = [normalized_name, " ".join(tokens[:2]) if len(tokens) >= 2 else None, tokens[0] if tokens else None]
    candidates_by_id: dict[str, Company] = {}

    for term in [value for value in search_terms if value]:
        rows = (
            db.query(Company)
            .filter(
                or_(
                    Company.name.ilike(f"%{term}%"),
                    Company.normalized_name.ilike(f"%{term}%"),
                )
            )
            .limit(40)
            .all()
        )
        for row in rows:
            candidates_by_id[str(row.id)] = row

    if not candidates_by_id:
        return None, 0.0

    ranked = []
    for candidate in candidates_by_id.values():
        score = _similarity(company_name, candidate.name)
        candidate_normalized = _normalize_company_name(candidate.name)
        if normalized_name == candidate_normalized:
            score = 1.0
        elif normalized_name in candidate_normalized or candidate_normalized in normalized_name:
            score = max(score, 0.93)
        ranked.append((score, candidate))

    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, best_company = ranked[0]
    runner_up_score = ranked[1][0] if len(ranked) > 1 else 0.0

    if best_score >= STRICT_MATCH_THRESHOLD and (best_score - runner_up_score >= 0.02 or best_score >= 0.97):
        return best_company, best_score

    return None, best_score


def _build_funding_id(release_url: str, company_name: str) -> str:
    slug = release_url.rstrip("/").split("/")[-1]
    normalized_company = _normalize_company_name(company_name).replace(" ", "-")
    return f"tedco:{slug}:{normalized_company}"


def _run_tedco_press_release_sync(db: Session, max_pages: Optional[int], persist: bool) -> TedcoFundingSyncResponse:
    try:
        first_page_html = _fetch_html(TEDCO_PRESS_RELEASES_URL.format(page=0))
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch TEDCO press releases: {exc}") from exc

    initial_links, discovered_last_page = _extract_listing_links(first_page_html)
    page_count = discovered_last_page + 1
    if max_pages is not None:
        page_count = max(1, min(page_count, max_pages))

    release_links = []
    seen_links = set(initial_links)
    release_links.extend(initial_links)

    for page in range(1, page_count):
        try:
            page_html = _fetch_html(TEDCO_PRESS_RELEASES_URL.format(page=page))
        except requests.RequestException:
            continue
        page_links, _ = _extract_listing_links(page_html)
        for link in page_links:
            if link not in seen_links:
                seen_links.add(link)
                release_links.append(link)

    response_items: list[TedcoFundingMatchItem] = []
    funding_candidates = 0
    matched_companies = 0
    inserted_records = 0
    skipped_records = 0

    for release_url in release_links:
        try:
            release_html = _fetch_html(release_url)
        except requests.RequestException:
            skipped_records += 1
            continue

        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", release_html, re.IGNORECASE | re.DOTALL)
        release_title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1) if title_match else "TEDCO Press Release"))).strip()
        release_text = _clean_html_to_text(release_html)

        candidates = _extract_release_candidates(release_title, release_text, release_url)
        if not candidates:
            continue

        for candidate in candidates:
            funding_candidates += 1
            matched_company, match_score = _match_company_in_database(candidate["company_name"], db)

            amount_awarded = float(candidate["amount_awarded"]) if candidate.get("amount_awarded") is not None else None
            release_date = candidate.get("release_date")
            release_date_str = release_date.date().isoformat() if release_date else None

            if not matched_company:
                skipped_records += 1
                response_items.append(
                    TedcoFundingMatchItem(
                        release_title=candidate["release_title"],
                        release_url=candidate["release_url"],
                        release_date=release_date_str,
                        extracted_company=candidate["company_name"],
                        amount_awarded=amount_awarded,
                        program_name=candidate.get("program_name"),
                        match_score=round(match_score, 3),
                        decision="skipped",
                        reason="No strict company match in database",
                    )
                )
                continue

            matched_companies += 1
            funding_id = _build_funding_id(candidate["release_url"], candidate["company_name"])
            decision = "matched"
            reason = candidate.get("reason") or "strict_match"

            if persist:
                existing = db.query(FundingRecord).filter(FundingRecord.funding_id == funding_id).first()
                if existing:
                    decision = "already_exists"
                    skipped_records += 1
                else:
                    db.add(
                        FundingRecord(
                            company_id=matched_company.id,
                            funding_id=funding_id,
                            funding_source="TEDCO",
                            funding_type="Press Release Funding",
                            funder_name="TEDCO",
                            program_name=candidate.get("program_name") or "TEDCO Press Release",
                            amount_awarded=candidate.get("amount_awarded"),
                            date_awarded=release_date.date() if release_date else None,
                            award_title=candidate["release_title"],
                            award_link=candidate["release_url"],
                            source_url=candidate["release_url"],
                            additional_info=f"Strict TEDCO press-release match ({reason}) with score {match_score:.3f}.",
                            abstract=release_text[:2000],
                        )
                    )
                    inserted_records += 1
                    decision = "synced"

            response_items.append(
                TedcoFundingMatchItem(
                    release_title=candidate["release_title"],
                    release_url=candidate["release_url"],
                    release_date=release_date_str,
                    extracted_company=candidate["company_name"],
                    matched_company_id=str(matched_company.id),
                    matched_company_name=matched_company.name,
                    amount_awarded=amount_awarded,
                    program_name=candidate.get("program_name"),
                    match_score=round(match_score, 3),
                    decision=decision,
                    reason=reason,
                )
            )

    if persist:
        db.commit()

    return TedcoFundingSyncResponse(
        success=True,
        pages_scanned=page_count,
        releases_scanned=len(release_links),
        funding_candidates=funding_candidates,
        matched_companies=matched_companies,
        inserted_records=inserted_records,
        skipped_records=skipped_records,
        strict_mode=True,
        items=response_items[:100],
    )


@router.get("/sam", response_model=IntegrationStubResponse)
async def sam_lookup(
    uei: Optional[str] = Query(None),
    duns: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
):
    if not uei and not duns and not name:
        raise HTTPException(status_code=400, detail="Provide uei, duns, or name")
    return IntegrationStubResponse(
        status="not_implemented",
        detail="SAM.gov integration stub",
        uei=uei,
        duns=duns,
        name=name,
    )


@router.get("/usaspending", response_model=IntegrationStubResponse)
async def usaspending_lookup(uei: str = Query(...)):
    return IntegrationStubResponse(
        status="not_implemented",
        detail="USAspending.gov integration stub",
        uei=uei,
    )


@router.get("/sbir", response_model=IntegrationStubResponse)
async def sbir_lookup(
    uei: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
):
    if not uei and not name:
        raise HTTPException(status_code=400, detail="Provide uei or name")
    return IntegrationStubResponse(
        status="not_implemented",
        detail="SBIR/STTR integration stub",
        uei=uei,
        name=name,
    )


@router.get("/uspto", response_model=IntegrationStubResponse)
async def uspto_lookup(name: str = Query(...)):
    return IntegrationStubResponse(
        status="not_implemented",
        detail="USPTO integration stub",
        name=name,
    )


@router.get("/tedco/press-releases/preview", response_model=TedcoFundingSyncResponse)
def preview_tedco_press_release_funding(
    max_pages: Optional[int] = Query(3, ge=1, le=58),
    db: Session = Depends(get_db),
):
    return _run_tedco_press_release_sync(db=db, max_pages=max_pages, persist=False)


@router.post("/tedco/press-releases/sync", response_model=TedcoFundingSyncResponse, status_code=status.HTTP_200_OK)
def sync_tedco_press_release_funding(
    max_pages: Optional[int] = Query(None, ge=1, le=58),
    db: Session = Depends(get_db),
):
    return _run_tedco_press_release_sync(db=db, max_pages=max_pages, persist=True)
