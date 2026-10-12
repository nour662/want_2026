"""
Funding source and stage classification rules.

Sources:
    SBIR, Grant, Venture Capital, MIPS, NSF I-Corps, Accelerator or Incubator,
    STTR, Competition or Prize, Purchase Order, Definitive Contract,
    Cooperative Agreement, Delivery Order, Angel, Equity Crowdfunding, Series, Other

Stages:
    Stage 1: Pre-Seed Funding
    Stage 2: Seed Funding
    Stage 3: Early Stage Investment (Series A & B)
    Stage 4: Later Stage Investment (Series C, D, etc.)
    Stage 5: Mezzanine Financing
    Stage 6: Revenue & Commercial Traction
"""

from decimal import Decimal
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Valid values
# ---------------------------------------------------------------------------

VALID_FUNDING_SOURCES = frozenset(
    {
        "SBIR",
        "Grant",
        "Venture Capital",
        "MIPS",
        "NSF I-Corps",
        "Accelerator or Incubator",
        "STTR",
        "Competition or Prize",
        "Purchase Order",
        "Definitive Contract",
        "Cooperative Agreement",
        "Delivery Order",
        "Angel",
        "Equity Crowdfunding",
        "Series",
        "Other",
    }
)

VALID_FUNDING_STAGES = (
    "Stage 1: Pre-Seed Funding",
    "Stage 2: Seed Funding",
    "Stage 3: Early Stage Investment (Series A & B)",
    "Stage 4: Later Stage Investment (Series C, D, etc.)",
    "Stage 5: Mezzanine Financing",
    "Stage 6: Revenue & Commercial Traction",
)

# ---------------------------------------------------------------------------
# Source classification
# ---------------------------------------------------------------------------

# Keywords in the raw funding_type / program / source string that map to a source
_SOURCE_KEYWORD_MAP = [
    ("NSF I-Corps", ["i-corps", "icorps"]),
    ("SBIR", ["sbir"]),
    ("STTR", ["sttr"]),
    ("MIPS", ["mips", "maryland industrial partnerships"]),
    ("NSF I-Corps", ["nsf i-corps"]),
    ("Accelerator or Incubator", ["accelerator", "incubator", "ycombinator", "y combinator", "techstars"]),
    ("Competition or Prize", ["competition", "prize", "challenge"]),
    ("Cooperative Agreement", ["cooperative agreement"]),
    ("Definitive Contract", ["definitive contract"]),
    ("Delivery Order", ["delivery order"]),
    ("Purchase Order", ["purchase order"]),
    ("Grant", ["grant", "nsf", "nih", "darpa", "doe", "dod", "federal"]),
    ("Angel", ["angel"]),
    ("Equity Crowdfunding", ["equity crowdfunding", "crowdfund"]),
    ("Venture Capital", ["venture capital", "vc ", " vc", "private equity"]),
    ("Series", ["series a", "series b", "series c", "series d", "series e", "series f"]),
]


def classify_funding_source(
    raw_source: Optional[str] = None,
    raw_type: Optional[str] = None,
    agency: Optional[str] = None,
    program: Optional[str] = None,
    phase: Optional[str] = None,
) -> str:
    """
    Determine the standardized funding source label.

    Priority order: explicit raw_source match → keyword scan across all fields → "Other"
    """
    # If already a valid source, keep it (normalise casing first)
    if raw_source:
        canonical = _normalise_to_valid_source(raw_source)
        if canonical:
            return canonical

    # Build a combined lowercase blob to keyword-scan
    blob = " ".join(
        filter(
            None,
            [
                str(raw_source or ""),
                str(raw_type or ""),
                str(agency or ""),
                str(program or ""),
                str(phase or ""),
            ],
        )
    ).lower()

    for source_label, keywords in _SOURCE_KEYWORD_MAP:
        if any(kw in blob for kw in keywords):
            return source_label

    return "Other"


def _normalise_to_valid_source(value: str) -> Optional[str]:
    """Return the canonical source string if value matches (case-insensitive)."""
    v = value.strip()
    for valid in VALID_FUNDING_SOURCES:
        if v.lower() == valid.lower():
            return valid
    return None


# ---------------------------------------------------------------------------
# Stage classification
# ---------------------------------------------------------------------------

_GRANT_SEED_THRESHOLD = Decimal("500000")
_COOP_SEED_THRESHOLD = Decimal("400000")
_VC_SEED_MAX = Decimal("1999999")
_VC_EARLY_MIN = Decimal("2000000")
_VC_EARLY_MAX = Decimal("9999999")
_VC_LATER_MIN = Decimal("10000000")

# Series letter → stage
_SERIES_STAGE_MAP = {
    "a": "Stage 3: Early Stage Investment (Series A & B)",
    "b": "Stage 3: Early Stage Investment (Series A & B)",
    "c": "Stage 4: Later Stage Investment (Series C, D, etc.)",
    "d": "Stage 4: Later Stage Investment (Series C, D, etc.)",
    "e": "Stage 4: Later Stage Investment (Series C, D, etc.)",
    "f": "Stage 4: Later Stage Investment (Series C, D, etc.)",
    "g": "Stage 4: Later Stage Investment (Series C, D, etc.)",
}


def classify_funding_stage(
    funding_source: str,
    amount: Optional[Decimal] = None,
    phase: Optional[str] = None,
    series_letter: Optional[str] = None,
) -> Optional[str]:
    """
    Return the standardised funding stage for a record.

    Parameters
    ----------
    funding_source:
        Already-classified source from ``classify_funding_source()``.
    amount:
        Award/investment amount in USD.
    phase:
        SBIR/STTR phase string, e.g. "Phase I", "Phase II", "1", "2".
    series_letter:
        Letter portion of a Series round ("A", "B", "C", …).
    """
    src = funding_source

    # ── Stage 1: Pre-Seed ──────────────────────────────────────────────────
    if src in ("NSF I-Corps", "Accelerator or Incubator", "Competition or Prize",
               "Angel", "Equity Crowdfunding"):
        return "Stage 1: Pre-Seed Funding"

    if src == "SBIR":
        if _is_phase(phase, 1):
            return "Stage 1: Pre-Seed Funding"
        if _is_phase(phase, 2):
            return "Stage 2: Seed Funding"

    if src == "STTR":
        if _is_phase(phase, 1):
            return "Stage 1: Pre-Seed Funding"
        if _is_phase(phase, 2):
            return "Stage 2: Seed Funding"

    if src == "Grant":
        if amount is not None:
            if amount <= _GRANT_SEED_THRESHOLD:
                return "Stage 1: Pre-Seed Funding"
            return "Stage 2: Seed Funding"

    if src == "Cooperative Agreement":
        if amount is not None:
            if amount <= _COOP_SEED_THRESHOLD:
                return "Stage 1: Pre-Seed Funding"
            return "Stage 2: Seed Funding"

    # ── Stage 2: Seed ──────────────────────────────────────────────────────
    if src == "MIPS":
        return "Stage 2: Seed Funding"

    # ── Series rounds ──────────────────────────────────────────────────────
    if src == "Series" or series_letter:
        letter = (series_letter or "").strip().lower()
        if not letter and src == "Series":
            # try to infer from phase/program field
            letter = _extract_series_letter(phase or "")
        stage = _SERIES_STAGE_MAP.get(letter)
        if stage:
            return stage

    # ── Venture Capital by amount ──────────────────────────────────────────
    if src == "Venture Capital":
        if amount is not None:
            if amount < Decimal("250000"):
                return "Stage 1: Pre-Seed Funding"
            if amount <= _VC_SEED_MAX:
                return "Stage 2: Seed Funding"
            if amount <= _VC_EARLY_MAX:
                return "Stage 3: Early Stage Investment (Series A & B)"
            return "Stage 4: Later Stage Investment (Series C, D, etc.)"

    # ── Stage 3: Early ─────────────────────────────────────────────────────
    if src == "Definitive Contract":
        return "Stage 3: Early Stage Investment (Series A & B)"

    # ── Stage 6: Revenue & Commercial Traction ─────────────────────────────
    if src in ("Purchase Order", "Delivery Order"):
        return "Stage 6: Revenue & Commercial Traction"

    return None


def classify_funding(
    raw_source: Optional[str] = None,
    amount: Optional[Decimal] = None,
    phase: Optional[str] = None,
    series_letter: Optional[str] = None,
    raw_type: Optional[str] = None,
    agency: Optional[str] = None,
    program: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """
    Single entry-point: classify both source and stage.

    Returns
    -------
    (funding_source, funding_stage)
    """
    source = classify_funding_source(
        raw_source=raw_source,
        raw_type=raw_type,
        agency=agency,
        program=program,
        phase=phase,
    )
    stage = classify_funding_stage(
        funding_source=source,
        amount=amount,
        phase=phase,
        series_letter=series_letter,
    )
    return source, stage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_phase(phase: Optional[str], number: int) -> bool:
    """Return True if the phase string refers to the given phase number."""
    if not phase:
        return False
    p = phase.strip().lower()
    return p in (str(number), f"phase {number}", f"phase{number}", f"i" if number == 1 else "ii")


def _extract_series_letter(text: str) -> str:
    """
    Try to extract a series letter from a free-text string.
    E.g. "Series A Funding" → "a"
    """
    import re
    match = re.search(r"\bseries\s+([a-g])\b", text.lower())
    return match.group(1) if match else ""
