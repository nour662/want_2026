"""
Company identity resolution.

Resolves a DUNS number to a UEI, or finds a UEI for a company by name
using SAM.gov search + a weighted scoring algorithm (threshold ≥ 80).
"""

import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

SBIR_BASE_URL = "https://api.www.sbir.gov/public/api"
SAM_ENTITY_URL = "https://api.sam.gov/entity-information/v3/entities"

_REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def resolve_uei_from_duns(duns: str) -> Optional[str]:
    """
    Use the SBIR.gov API to map a DUNS number to a UEI.

    Returns the UEI string if found, otherwise None.
    """
    duns = duns.strip()
    if not duns:
        return None

    try:
        resp = requests.get(
            f"{SBIR_BASE_URL}/firm",
            params={"duns": duns, "rows": 1, "start": 0},
            timeout=_REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
        firms = _extract_list(payload)
        if firms:
            uei = firms[0].get("uei") or firms[0].get("UEI")
            if uei:
                return str(uei).strip()
    except Exception:
        logger.exception("DUNS→UEI resolution failed for DUNS %s", duns)

    return None


def resolve_uei_from_name(
    company_name: str,
    *,
    website: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
) -> Optional[str]:
    """
    Search SAM.gov by company name and return the best-matching UEI
    if the weighted confidence score is ≥ 80.

    Scoring weights
    ---------------
    Exact normalised name match   : 45 pts
    Fuzzy name match (token-based): 0–30 pts
    Website domain match          : 20 pts
    City + state match            : 10 pts  (5 each)
    """
    api_key = os.getenv("SAM_API_KEY")
    if not api_key:
        logger.warning("SAM_API_KEY not set; skipping name-based UEI resolution")
        return None

    name = company_name.strip()
    if not name:
        return None

    try:
        resp = requests.get(
            SAM_ENTITY_URL,
            params={
                "api_key": api_key,
                "legalBusinessName": name,
                "includeSections": "entityRegistration",
                "format": "json",
                "size": 10,
            },
            timeout=_REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        logger.exception("SAM.gov name search failed for '%s'", name)
        return None

    candidates: List[Dict[str, Any]] = (
        payload.get("entityData")
        or payload.get("data")
        or []
    )

    if not candidates:
        return None

    best_uei: Optional[str] = None
    best_score = 0

    caller_domain = _extract_domain(website or "")
    caller_city = (city or "").strip().lower()
    caller_state = (state or "").strip().lower()
    norm_caller_name = _normalise_name(name)

    for candidate in candidates[:10]:
        reg = (
            candidate.get("entityRegistration")
            or candidate.get("registration")
            or candidate
        )
        uei = reg.get("ueiSAM") or reg.get("uei") or reg.get("UEI")
        if not uei:
            continue

        candidate_name = (
            reg.get("legalBusinessName")
            or reg.get("legalName")
            or ""
        )
        candidate_website = (
            reg.get("entityURL")
            or reg.get("website")
            or ""
        )
        candidate_city = (reg.get("physicalAddress") or {}).get("city", "") or ""
        candidate_state = (reg.get("physicalAddress") or {}).get("stateOrProvinceCode", "") or ""

        score = _score_candidate(
            norm_caller_name=norm_caller_name,
            caller_domain=caller_domain,
            caller_city=caller_city,
            caller_state=caller_state,
            candidate_name=candidate_name,
            candidate_website=candidate_website,
            candidate_city=candidate_city,
            candidate_state=candidate_state,
        )

        if score > best_score:
            best_score = score
            best_uei = str(uei).strip()

    if best_score >= 80:
        logger.info(
            "Name-based UEI resolved for '%s': UEI=%s (score=%d)",
            name,
            best_uei,
            best_score,
        )
        return best_uei

    logger.debug(
        "Name-based UEI resolution below threshold for '%s': best_score=%d",
        name,
        best_score,
    )
    return None


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _score_candidate(
    norm_caller_name: str,
    caller_domain: str,
    caller_city: str,
    caller_state: str,
    candidate_name: str,
    candidate_website: str,
    candidate_city: str,
    candidate_state: str,
) -> int:
    score = 0
    norm_candidate_name = _normalise_name(candidate_name)

    # Exact normalised name match
    if norm_caller_name and norm_caller_name == norm_candidate_name:
        score += 45
    else:
        # Token overlap score (Jaccard-style), scaled to 0–30
        score += int(_token_overlap(norm_caller_name, norm_candidate_name) * 30)

    # Website domain match
    if caller_domain and caller_domain == _extract_domain(candidate_website):
        score += 20

    # City match
    if caller_city and caller_city == candidate_city.strip().lower():
        score += 5

    # State match
    if caller_state and caller_state == candidate_state.strip().lower():
        score += 5

    return min(score, 100)


def _normalise_name(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation for comparison."""
    name = name.lower().strip()
    # Remove common legal suffixes
    for suffix in (
        r"\bllc\b", r"\binc\.?", r"\bcorp\.?", r"\bltd\.?",
        r"\bco\.?\b", r"\blp\b", r"\bllp\b", r"\bpbc\b",
    ):
        name = re.sub(suffix, "", name)
    name = re.sub(r"[^a-z0-9\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _token_overlap(a: str, b: str) -> float:
    """Return Jaccard similarity between word-token sets."""
    set_a = set(a.split())
    set_b = set(b.split())
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def _extract_domain(url: str) -> str:
    """Extract the bare hostname (without www.) from a URL."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host.removeprefix("www.").lower()
    except Exception:
        return ""


def _extract_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "items", "firms", "companies"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []
