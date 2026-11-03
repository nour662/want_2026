"""
TEDCO (Maryland Technology Development Corp.) press releases.

Two phases:

1. ``TedcoArchive.refresh()`` runs once per pipeline run. It crawls the
   articles listing on tedcomd.com and caches every article (title, date,
   text) in a JSONL file, so later runs only fetch articles that are new.
   The CSV produced by the old standalone ``tedco_scraper`` can be imported
   as a seed (it carries the old site's "Related Companies" tags).

2. ``find_funding(company)`` runs per company in parallel. It finds articles
   that mention the company, scores how sure the mention is, and extracts the
   investment amount and TEDCO fund named closest to the mention.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from app.services.watn_pipeline import http
from app.services.watn_pipeline.matching import (
    PROBABLE,
    REVIEW,
    find_name_in_text,
    is_generic_name,
    normalize_name,
    tier_for,
)
from app.services.watn_pipeline.models import CompanyInput, FundingRecord
from app.services.watn_pipeline.normalize import parse_amount, parse_date

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tedcomd.com"
LISTING_URL = BASE_URL + "/impact-news/articles-announcements"
MAX_LISTING_PAGES = 200

# Ordered most-specific first; value = (canonical fund name, funding stage or None)
TEDCO_FUNDS: List[Tuple[str, str, Optional[str]]] = [
    ("pre-seed builder fund", "Pre-Seed Builder Fund", "Stage 1: Pre-Seed Funding"),
    ("builder fund", "Builder Fund", "Stage 1: Pre-Seed Funding"),
    ("concept capital", "Concept Capital", "Stage 1: Pre-Seed Funding"),
    ("maryland innovation initiative", "Maryland Innovation Initiative", "Stage 1: Pre-Seed Funding"),
    ("rural business innovation initiative", "Rural Business Innovation Initiative", "Stage 1: Pre-Seed Funding"),
    ("social impact fund", "Social Impact Funds", "Stage 1: Pre-Seed Funding"),
    ("inclusion fund", "Inclusion Fund", "Stage 2: Seed Funding"),
    ("seed fund", "Seed Fund", "Stage 2: Seed Funding"),
    ("venture fund", "Venture Fund", None),
    ("maryland stem cell research fund", "Maryland Stem Cell Research Fund", None),
    ("equitech growth fund", "Equitech Growth Fund", None),
    ("technology commercialization fund", "Technology Commercialization Fund", "Stage 1: Pre-Seed Funding"),
    ("propel baltimore fund", "Propel Baltimore Fund", "Stage 1: Pre-Seed Funding"),
    ("gap fund", "Gap Fund", "Stage 1: Pre-Seed Funding"),
]

FUNDING_WORDS = re.compile(
    r"\b(invest(?:ed|ment|s)?|award(?:ed|s)?|fund(?:ed|ing)?|grant(?:ed|s)?|receiv(?:ed|es)|"
    r"prize|winner|won|portfolio|financing|capital)\b",
    re.I,
)
NON_FUNDING_TITLES = re.compile(r"meeting notice|commission -|board of directors meeting|agenda", re.I)
AMOUNT_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(million|billion|thousand|[mk])?\b", re.I)
SENTENCE_END = re.compile(r"[.!?]\s+(?=[A-Z\u201c\"])")
AMOUNT_WINDOW = 220  # characters around a mention in which an amount is attributed to it
MIN_SINGLE_INVESTMENT = 1_000
MAX_SINGLE_INVESTMENT = 2_500_000  # larger figures in press releases are fund/program totals
PARTICIPANT_LOOKBACK_YEARS = 2  # participant-only matches must be this close to the cohort year


@dataclass
class Article:
    url: str
    title: str
    published: Optional[str]  # ISO date
    text: str
    related_companies: List[str] = field(default_factory=list)
    related_funds: List[str] = field(default_factory=list)


class TedcoArchive:
    def __init__(self, cache_dir: Path, seed_csv: Optional[Path] = None, max_new_pages: int = MAX_LISTING_PAGES):
        self.cache_path = Path(cache_dir) / "tedco_articles.jsonl"
        self.seed_csv = Path(seed_csv) if seed_csv else None
        self.max_new_pages = max_new_pages
        self.articles: Dict[str, Article] = {}
        self._lock = threading.Lock()
        self.error: Optional[str] = None
        self.crawl_error: Optional[str] = None

    # ── Crawl / cache ────────────────────────────────────────────────

    def refresh(self, crawl: bool = True) -> None:
        self._load_cache()
        if self.seed_csv and self.seed_csv.exists():
            self._import_seed(self.seed_csv)
        if crawl:
            try:
                self._crawl()
            except Exception as exc:
                blocked = "403" in str(exc)
                logger.warning(
                    "TEDCO crawl %s; continuing with %d cached articles",
                    "blocked by tedcomd.com bot protection (HTTP 403)" if blocked else f"failed: {exc}",
                    len(self.articles),
                )
                self.crawl_error = "blocked by site bot protection (HTTP 403)" if blocked else str(exc)
        logger.info("TEDCO archive ready: %d articles", len(self.articles))

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        with open(self.cache_path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    art = Article(**json.loads(line))
                    self.articles[art.url] = art

    def _append_cache(self, articles: Iterable[Article]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.cache_path, "a", encoding="utf-8") as fh:
            for art in articles:
                fh.write(json.dumps(asdict(art)) + "\n")

    def _import_seed(self, path: Path) -> None:
        new = []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                url = (row.get("URL") or "").strip()
                if not url or url in self.articles:
                    continue
                published = parse_date(row.get("Release Date"))
                art = Article(
                    url=url,
                    title=(row.get("Title") or "").replace("| TEDCO", "").strip(),
                    published=published.isoformat() if published else None,
                    text=row.get("Content") or "",
                    related_companies=_split(row.get("Related Companies")),
                    related_funds=_split(row.get("Related Funds")),
                )
                self.articles[url] = art
                new.append(art)
        if new:
            self._append_cache(new)
            logger.info("Imported %d TEDCO articles from seed %s", len(new), path)

    def _crawl(self) -> None:
        from lxml import html as lhtml

        to_fetch: List[str] = []
        for page in range(self.max_new_pages):
            resp = http.request("GET", LISTING_URL, params={"page": page}, timeout=30)
            tree = lhtml.fromstring(resp.content)
            links = tree.xpath('//article[contains(@class,"node-teaser")]//div[contains(@class,"node-teaser__heading")]//a/@href')
            links = [link if link.startswith("http") else BASE_URL + link for link in links]
            if not links:
                break
            fresh = [link for link in links if link not in self.articles and link not in to_fetch]
            to_fetch.extend(fresh)
            if not fresh and self.articles:
                break  # listing is newest-first: everything older is already cached
        if not to_fetch:
            return
        logger.info("Fetching %d new TEDCO articles", len(to_fetch))
        with ThreadPoolExecutor(max_workers=4) as pool:
            fetched = [a for a in pool.map(self._fetch_article, to_fetch) if a]
        for art in fetched:
            self.articles[art.url] = art
        self._append_cache(fetched)

    @staticmethod
    def _fetch_article(url: str) -> Optional[Article]:
        from lxml import html as lhtml

        try:
            tree = lhtml.fromstring(http.request("GET", url, timeout=30).content)
        except Exception as exc:
            logger.warning("TEDCO article fetch failed %s: %s", url, exc)
            return None
        for bad in tree.xpath("//script|//style|//noscript"):
            if bad.tag != "script" or "ld+json" not in (bad.get("type") or ""):
                bad.drop_tree()
        title = " ".join(tree.xpath('string(//h1[contains(@class,"node__heading")])').split()) or " ".join(
            tree.xpath("string(//title)").replace("| TEDCO", "").split()
        )
        published = None
        for blob in tree.xpath('//script[@type="application/ld+json"]/text()'):
            match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', blob)
            if match:
                published = parse_date(match.group(1)[:10])
                break
        body = tree.xpath('//div[contains(@class,"node__content")]') or tree.xpath("//article") or [tree]
        text = " ".join(body[0].text_content().split())
        if published is None:
            match = re.search(r"\(([A-Z][a-z]+ \d{1,2}, \d{4})\)", text)
            published = parse_date(match.group(1)) if match else None
        return Article(url=url, title=title, published=published.isoformat() if published else None, text=text)

    # ── Per-company matching ──────────────────────────────────────────

    def find_funding(self, company: CompanyInput, since: Optional[date] = None) -> List[FundingRecord]:
        records: List[FundingRecord] = []
        names = [company.name]
        for art in self.articles.values():
            if NON_FUNDING_TITLES.search(art.title):
                continue
            published = date.fromisoformat(art.published) if art.published else None
            if since and published and published < since:
                continue
            confidence, how = _mention_confidence(names, company.contacts, art)
            if confidence < REVIEW:
                continue
            if how.startswith("participant") and company.cohort_date and published and \
                    published < date(company.cohort_date.year - PARTICIPANT_LOOKBACK_YEARS, 1, 1):
                continue  # a person's name in an article years before the company existed
            if not FUNDING_WORDS.search(art.title + " " + art.text):
                continue
            amount, fund, stage, snippet = _extract_award(company.name, art)
            if amount is None and not (snippet and FUNDING_WORDS.search(snippet)):
                continue  # mentioned in passing (lists, events), not a funding announcement
            reasons = []
            if amount is not None and amount < MIN_SINGLE_INVESTMENT:
                reasons.append(f"TEDCO amount ${amount:,.0f} is implausibly small; check the article")
            if amount is not None and amount > MAX_SINGLE_INVESTMENT:
                reasons.append(f"TEDCO amount ${amount:,.0f} looks like a program total, not one investment")
            if tier_for(confidence) == "review":
                reasons.append(f"TEDCO mention needs review ({how})")
            if amount is None:
                reasons.append("TEDCO article names company but no amount found near the mention")
            records.append(
                FundingRecord(
                    source="tedco",
                    amount=amount,
                    date_awarded=published,
                    title=art.title,
                    investor="TEDCO" + (f" - {fund}" if fund else ""),
                    program=fund,
                    raw_type="TEDCO",
                    funding_source="TEDCO",
                    funding_stage=stage,
                    source_url=art.url,
                    additional_info=(f"TEDCO press release: {snippet}" if snippet else None),
                    recipient_name=company.name,
                    confidence=confidence,
                    match_method=f"tedco:{how}",
                    review_reasons=reasons,
                )
            )
        return records


def _split(value: Optional[str]) -> List[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _mention_confidence(names: List[str], contacts: List[str], art: Article) -> Tuple[float, str]:
    for name in names:
        norm = normalize_name(name)
        if any(normalize_name(rc) == norm for rc in art.related_companies):
            return 97.0, "tagged company"
        generic = is_generic_name(norm)
        in_title, in_body = find_name_in_text(name, art.title), find_name_in_text(name, art.text)
        if not generic:
            if in_title:
                return 92.0, "name in title"
            if in_body:
                return 85.0, "name in body"
        elif in_title or in_body:
            # Generic names ("Phase", "Start", "Press") are ordinary words: only a
            # participant named in the same article makes the mention count.
            if any(find_name_in_text(c, art.title + " " + art.text) for c in contacts if len(c.split()) >= 2):
                return PROBABLE, "generic name + participant named"
            return 0.0, ""
    if any(find_name_in_text(c, art.title + " " + art.text) for c in contacts if len(c.split()) >= 2):
        return REVIEW, "participant named, company not named"
    return 0.0, ""


def _mention_positions(name: str, text: str) -> List[int]:
    tokens = [re.escape(t) for t in normalize_name(name).split()]
    if not tokens:
        return []
    pattern = re.compile(r"\b" + r"[\W_]+".join(tokens) + r"\b", re.I)
    return [m.start() for m in pattern.finditer(text)]


def _extract_award(name: str, art: Article) -> Tuple[Optional[float], Optional[str], Optional[str], Optional[str]]:
    text = art.text
    positions = _mention_positions(name, text)
    amount: Optional[float] = None
    snippet: Optional[str] = None
    anchor = positions[0] if positions else None
    best_distance = float("inf")
    for pos in positions:
        # Prefer the first amount after the mention in the same sentence ("Acme ... received
        # $50,000"); in lists the amount just before the name belongs to the previous company.
        end_match = SENTENCE_END.search(text, pos)
        sentence_end = min(end_match.start() + 1 if end_match else len(text), pos + AMOUNT_WINDOW)
        after = AMOUNT_RE.search(text, pos, sentence_end)
        if after:
            candidate, distance = after, after.start() - pos
        else:
            start_match = None
            for start_match in SENTENCE_END.finditer(text, max(0, pos - AMOUNT_WINDOW), pos):
                pass
            sentence_start = start_match.end() - 1 if start_match else max(0, pos - AMOUNT_WINDOW)
            before = list(AMOUNT_RE.finditer(text, sentence_start, pos))
            if not before:
                continue
            candidate, distance = before[-1], AMOUNT_WINDOW + (pos - before[-1].end())
        if distance < best_distance:
            best_distance = distance
            amount = parse_amount(candidate.group(1) + (candidate.group(2) or ""))
            anchor = pos
    if anchor is not None:
        snippet = text[max(0, anchor - 80): anchor + 200].strip()

    fund, stage = _nearest_fund(art, anchor)
    if stage is None and fund == "Venture Fund" and amount is not None:
        stage = "Stage 2: Seed Funding" if amount < 2_000_000 else "Stage 3: Early Stage Investment (Series A & B)"
    return amount, fund, stage, snippet


def _nearest_fund(art: Article, anchor: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    for tagged in art.related_funds:
        for needle, canonical, stage in TEDCO_FUNDS:
            if needle in tagged.lower():
                return canonical, stage
    lowered = art.text.lower()
    best: Tuple[float, Optional[str], Optional[str]] = (float("inf"), None, None)
    for needle, canonical, stage in TEDCO_FUNDS:
        if needle in art.title.lower():
            return canonical, stage
        for match in re.finditer(re.escape(needle), lowered):
            distance = abs(match.start() - anchor) if anchor is not None else match.start()
            if distance < best[0]:
                best = (distance, canonical, stage)
    return best[1], best[2]
