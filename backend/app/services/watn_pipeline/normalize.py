"""Small value parsers shared by every source."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Optional

_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%d-%b-%Y", "%B %d, %Y",
    "%A, %B %d, %Y", "%b %d, %Y",
)


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in {"", "nan", "none", "null", "nat", "<na>"}


def clean_str(value: Any) -> Optional[str]:
    return None if is_blank(value) else re.sub(r"\s+", " ", str(value)).strip()


def parse_date(value: Any) -> Optional[date]:
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_amount(value: Any) -> Optional[float]:
    if is_blank(value):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value).strip().lower().replace(",", "").replace("$", "")
    multiplier = 1.0
    match = re.fullmatch(r"([\d.]+)\s*(million|m|billion|b|thousand|k)?", text)
    if not match:
        try:
            return float(text)
        except ValueError:
            return None
    number, unit = match.groups()
    if unit in ("million", "m"):
        multiplier = 1e6
    elif unit in ("billion", "b"):
        multiplier = 1e9
    elif unit in ("thousand", "k"):
        multiplier = 1e3
    try:
        return round(float(number) * multiplier, 2)
    except ValueError:
        return None
