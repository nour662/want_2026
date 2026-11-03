"""
Entity matching and scoring.

Every decision that links a remote record to an input company goes through
``score_entity`` so it is explainable (reasons) and tiered:

    confirmed  >= 90   identifier match, or strong name + corroboration
    probable   >= 80   accepted automatically
    review     >= 65   kept out of the output, written to the review queue
    rejected   <  65
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Iterable, List, Optional

from rapidfuzz import fuzz

from app.services.watn_pipeline.models import MatchEvidence

CONFIRMED = 90.0
PROBABLE = 80.0
REVIEW = 65.0

_LEGAL_SUFFIXES = (
    "incorporated", "inc", "llc", "l l c", "ltd", "limited", "corporation", "corp",
    "co", "company", "pllc", "plc", "lp", "llp", "pbc", "pc",
)
_SUFFIX_RE = re.compile(r"\b(?:%s)\b\.?" % "|".join(re.escape(s) for s in _LEGAL_SUFFIXES))
_GENERIC_WORDS = {
    "technologies", "technology", "tech", "solutions", "systems", "labs", "lab",
    "group", "the", "and", "of", "bio", "health", "therapeutics", "sciences",
    "medical", "innovations", "services", "consulting", "international", "usa",
}

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    "puerto rico": "PR", "virgin islands": "VI",
}


def normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    text = str(name).lower().replace("&", " and ")
    text = re.sub(r"\([^)]*\)", " ", text)  # "Acme (fka Foo)" -> "acme"
    text = re.sub(r"[^\w\s]", " ", text)
    text = _SUFFIX_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = "".join(ch for ch in str(value).upper() if ch.isalnum())
    if not text or text in {"NAN", "NONE", "NULL"}:
        return None
    return text


def normalize_duns(value: Optional[str]) -> Optional[str]:
    text = normalize_id(value)
    if not text:
        return None
    if text.endswith("0") and "." in str(value):  # "118090977.0" from pandas floats
        text = str(value).split(".")[0]
    digits = re.sub(r"\D", "", text)
    return digits.zfill(9) if digits else None


def normalize_state(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 2:
        return text.upper()
    return US_STATES.get(text.lower())


def extract_domain(url: Optional[str]) -> str:
    if not url or str(url).strip().lower() in {"nan", "none"}:
        return ""
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def is_generic_name(normalized: str) -> bool:
    """Short or dictionary-word names that collide with unrelated entities."""
    tokens = [t for t in normalized.split() if t not in _GENERIC_WORDS]
    if not tokens:
        return True
    return len(tokens) == 1 and len(tokens[0]) <= 5


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    if na.replace(" ", "") == nb.replace(" ", ""):
        return 98.0
    ratio = fuzz.ratio(na, nb)
    token_sort = fuzz.token_sort_ratio(na, nb)
    # token_set rewards subset matches ("acme" vs "acme robotics"); discount it
    token_set = fuzz.token_set_ratio(na, nb) * 0.9
    return float(max(ratio, token_sort, token_set))


SUBSET_CAP = 79.0  # "Kestrel" vs "Kestrel Intelligence": needs website/state/contact to pass
INPUT_EXTRA_CAP = 72.0  # input has a distinctive word the registered name lacks
SUBSTITUTION_CAP = 70.0  # "Smart Wand" vs "Smart Pad": different companies sharing words


def _tokens(name: str) -> List[str]:
    return normalize_name(name).split()


def _token_cap(a: str, b: str) -> float:
    """
    Upper bound on the name score from words present on one side only.

    Generic words ("therapeutics", "technologies") count as extra words but
    cannot make two names match on their own.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 100.0

    def unmatched(src: List[str], dst: List[str]) -> List[str]:
        return [t for t in src if max(fuzz.ratio(t, d) for d in dst) < 85]

    only_a, only_b = unmatched(ta, tb), unmatched(tb, ta)
    distinct_a = [t for t in ta if t not in _GENERIC_WORDS]
    distinct_b = [t for t in tb if t not in _GENERIC_WORDS]
    only_a_d = [t for t in only_a if t not in _GENERIC_WORDS]
    only_b_d = [t for t in only_b if t not in _GENERIC_WORDS]

    if bool(distinct_a) != bool(distinct_b):
        return SUBSTITUTION_CAP  # "Avcentx Therapeutics" vs "LP Therapeutics" (-> "therapeutics")
    if only_a_d and only_b_d:
        return SUBSTITUTION_CAP  # "Smart Wand" vs "Smart Pad", "S Neuro" vs "T-Neuro"
    if only_a_d:
        return INPUT_EXTRA_CAP  # "StormWork Tool" vs "Tool, Inc."
    if only_b:
        return SUBSET_CAP  # "Scribe" vs "Scribe Therapeutics": needs corroboration
    if only_a:
        return 85.0  # input carries an extra generic word
    return 100.0


def tier_for(score: float) -> str:
    if score >= CONFIRMED:
        return "confirmed"
    if score >= PROBABLE:
        return "probable"
    if score >= REVIEW:
        return "review"
    return "rejected"


def is_accepted(evidence: Optional[MatchEvidence]) -> bool:
    return bool(evidence) and evidence.tier in ("confirmed", "probable")


def score_entity(
    *,
    input_name: str,
    candidate_names: Iterable[Optional[str]],
    input_uei: Optional[str] = None,
    candidate_uei: Optional[str] = None,
    input_duns: Optional[str] = None,
    candidate_duns: Optional[str] = None,
    input_website: Optional[str] = None,
    candidate_website: Optional[str] = None,
    input_state: Optional[str] = None,
    candidate_state: Optional[str] = None,
    input_contacts: Iterable[str] = (),
    candidate_contacts: Iterable[str] = (),
) -> MatchEvidence:
    reasons: List[str] = []

    if normalize_id(input_uei) and normalize_id(input_uei) == normalize_id(candidate_uei):
        return MatchEvidence(100.0, "confirmed", "uei", ["UEI matches input"])
    if normalize_duns(input_duns) and normalize_duns(input_duns) == normalize_duns(candidate_duns):
        return MatchEvidence(98.0, "confirmed", "duns", ["DUNS matches input"])

    names = [n for n in candidate_names if n]
    best_name, name_score = "", 0.0
    for candidate in names:
        s = min(name_similarity(input_name, candidate), _token_cap(input_name, candidate))
        if s > name_score:
            best_name, name_score = candidate, s
    score = name_score
    reasons.append(f"name {name_score:.0f} vs '{best_name}'")
    if name_score < 100 and _token_cap(input_name, best_name) < 100:
        reasons.append("distinctive words differ" if _token_cap(input_name, best_name) <= SUBSTITUTION_CAP
                       else "one name is a subset of the other")
    method = "name"

    in_domain, cand_domain = extract_domain(input_website), extract_domain(candidate_website)
    corroborated = False
    if in_domain and cand_domain:
        if in_domain == cand_domain:
            score += 15
            corroborated = True
            method = "name+domain"
            reasons.append("website domain matches")
        else:
            score -= 10
            reasons.append(f"website differs ({in_domain} vs {cand_domain})")

    in_state, cand_state = normalize_state(input_state), normalize_state(candidate_state)
    if in_state and cand_state:
        if in_state == cand_state:
            score += 5
            corroborated = True
            reasons.append("state matches")
        else:
            score -= 10
            reasons.append(f"state differs ({in_state} vs {cand_state})")

    contacts_in = [c for c in input_contacts if c and c.strip()]
    contacts_cand = [c for c in candidate_contacts if c and c.strip()]
    if contacts_in and contacts_cand:
        best_contact = max(fuzz.token_sort_ratio(a.lower(), b.lower()) for a in contacts_in for b in contacts_cand)
        if best_contact >= 85:
            score += 10
            corroborated = True
            method += "+contact"
            reasons.append("point of contact matches a participant")

    score = max(0.0, min(score, 100.0))
    if is_generic_name(normalize_name(input_name)) and not corroborated and score >= REVIEW:
        score = min(score, PROBABLE - 1)
        reasons.append("short/generic name without corroboration")

    return MatchEvidence(round(score, 1), tier_for(score), method, reasons)


def find_name_in_text(name: str, text: str) -> bool:
    """Whole-word, case-insensitive search for a company or person name in free text."""
    target = normalize_name(name)
    if not target or len(target) < 3:
        return False
    haystack = " " + normalize_name(text) + " "
    return f" {target} " in haystack
