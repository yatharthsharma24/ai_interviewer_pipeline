"""Coercion of free-text form answers into the typed columns on ``Candidate``.

Google Forms hands back strings for everything, and candidates type whatever they like
("2.5 yrs", "immediate", "12 LPA"). These helpers are deliberately forgiving: a value we
cannot parse becomes ``None`` and is treated as a missing field, never as a zero.
"""

from __future__ import annotations

import re

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

_IMMEDIATE_WORDS = ("immediate", "immediately", "available now", "asap", "serving", "none")

_CTC_SUFFIXES: tuple[tuple[tuple[str, ...], float], ...] = (
    (("cr", "crore", "crores"), 10_000_000.0),
    (("lpa", "lakh", "lakhs", "lac", "lacs", "l"), 100_000.0),
    (("k",), 1_000.0),
)


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_number(value: str | None) -> float | None:
    """First number in the string, tolerating '3.5 years', '~4', '2,5'."""
    if value is None:
        return None
    match = _NUMBER_RE.search(value)
    if not match:
        return None
    try:
        return float(match.group().replace(",", "."))
    except ValueError:
        return None


def parse_years(value: str | None) -> float | None:
    """Years of experience. Handles 'fresher' and 'X years Y months'."""
    if value is None:
        return None
    lowered = value.lower()
    if any(word in lowered for word in ("fresher", "fresh graduate", "no experience")):
        return 0.0

    years_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+)?\s*(?:years?|yrs?|y\b)", lowered)
    months_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:months?|mos?|m\b)", lowered)
    if years_match or months_match:
        total = float(years_match.group(1)) if years_match else 0.0
        if months_match:
            total += float(months_match.group(1)) / 12.0
        return round(total, 2)

    return parse_number(value)


def parse_notice_period_days(value: str | None) -> int | None:
    """Normalise notice period to days. 'Immediate' -> 0, '2 months' -> 60."""
    if value is None:
        return None
    lowered = value.lower()
    if any(word in lowered for word in _IMMEDIATE_WORDS):
        return 0

    number = parse_number(lowered)
    if number is None:
        return None
    if re.search(r"month|mon\b|mth", lowered):
        return int(round(number * 30))
    if re.search(r"week|wk", lowered):
        return int(round(number * 7))
    return int(round(number))


def parse_currency(value: str | None) -> float | None:
    """Compensation with Indian/Western shorthand. '12 LPA' -> 1200000, '90k' -> 90000."""
    if value is None:
        return None
    lowered = value.lower().replace(",", "")
    number = parse_number(lowered)
    if number is None:
        return None

    for suffixes, multiplier in _CTC_SUFFIXES:
        for suffix in suffixes:
            if re.search(rf"\d\s*{re.escape(suffix)}\b", lowered):
                return number * multiplier
    return number


def parse_list(values: list[str]) -> list[str]:
    """Checkbox answers arrive as many values; text answers arrive as one comma-joined string."""
    items: list[str] = []
    for value in values:
        for part in re.split(r"[,;/|\n]+", value):
            cleaned = part.strip()
            if cleaned:
                items.append(cleaned)

    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def looks_like_email(value: str | None) -> bool:
    return bool(value) and bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", value.strip()))


def looks_like_phone(value: str | None) -> bool:
    if not value:
        return False
    digits = re.sub(r"\D", "", value)
    return 7 <= len(digits) <= 15


def looks_like_url(value: str | None) -> bool:
    if not value:
        return False
    return bool(re.match(r"https?://\S+\.\S+", value.strip(), flags=re.IGNORECASE))
