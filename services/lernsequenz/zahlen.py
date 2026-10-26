"""Numbers the way people type them.

German reading first (the courses are de-DE): a dot before three digits is a thousands
separator, a comma is the decimal mark. The one genuinely ambiguous shape, a single
separator followed by exactly three digits (``45.000`` / ``45,000``), is flagged together
with the other reading so the player can ask instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NumberReading:
    value: float
    display: str
    ambiguous: bool
    alternative: float | None
    percent: bool


_PREFIX = re.compile(r"^(ca\.?|circa|etwa|rund|~|≈)\s*", re.IGNORECASE)
_RANGE = re.compile(r"\d\s*[-–]\s*\d")
_DATE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$")
_MULTIPLIER = re.compile(r"^(?P<core>.*?\d)\s*(?P<mult>k|tsd\.?|t€|mio\.?|m)\s*$", re.IGNORECASE)
_MULTIPLIERS = {"k": 1_000.0, "tsd": 1_000.0, "tsd.": 1_000.0, "t€": 1_000.0, "mio": 1_000_000.0, "mio.": 1_000_000.0, "m": 1_000_000.0}
_CURRENCY = re.compile(r"\s*(€|eur|euro)\s*", re.IGNORECASE)
_PER_PERIOD = re.compile(r"\s*/\s*[a-zäöü.]+\s*$", re.IGNORECASE)
_TIME_UNIT = re.compile(r"\s*(h|std\.?|stunden?)$", re.IGNORECASE)
_AMBIGUOUS = re.compile(r"^-?\d{1,3}([.,])\d{3}$")
_THOUSANDS_DOT = re.compile(r"^-?\d{1,3}(\.\d{3})+$")
_THOUSANDS_COMMA = re.compile(r"^-?\d{1,3}(,\d{3})+$")
_PLAIN = re.compile(r"^-?\d+([.,]\d+)?$")


def read_number(text: object) -> NumberReading | None:
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return NumberReading(float(text), format_number(float(text)), False, None, False)
    if not isinstance(text, str):
        return None
    cleaned = _PREFIX.sub("", text.strip())
    if not cleaned or _RANGE.search(cleaned) or _DATE.fullmatch(cleaned):
        return None
    percent = "%" in cleaned
    cleaned = cleaned.replace("%", "")
    cleaned = _PER_PERIOD.sub("", cleaned)
    factor = 1.0
    multiplied = _MULTIPLIER.fullmatch(cleaned)
    if multiplied:
        factor = _MULTIPLIERS[multiplied.group("mult").lower()]
        cleaned = multiplied.group("core")
    cleaned = _CURRENCY.sub("", cleaned)
    cleaned = _TIME_UNIT.sub("", cleaned).strip()
    core = re.sub(r"(?<=\d)\s+(?=\d{3}\b)", "", cleaned)
    reading = _read_core(core)
    if reading is None:
        return None
    value, ambiguous, alternative = reading
    value *= factor
    alternative = alternative * factor if alternative is not None else None
    return NumberReading(value, format_number(value), ambiguous, alternative, percent)


def _read_core(core: str) -> tuple[float, bool, float | None] | None:
    """(value, ambiguous, alternative) for the bare number text, German reading first."""
    ambiguous = _AMBIGUOUS.fullmatch(core)
    if ambiguous:
        digits = float(core.replace(".", "").replace(",", ""))
        decimal = float(core.replace(",", "."))
        return (digits, True, decimal) if ambiguous.group(1) == "." else (decimal, True, digits)
    if "." in core and "," in core:
        decimal = "," if core.rfind(",") > core.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        core = core.replace(thousands, "").replace(decimal, ".")
    elif _THOUSANDS_DOT.fullmatch(core):
        core = core.replace(".", "")
    elif _THOUSANDS_COMMA.fullmatch(core):
        core = core.replace(",", "")
    elif "," in core:
        core = core.replace(",", ".")
    if not _PLAIN.fullmatch(core):
        return None
    return float(core), False, None


def why_not_a_number(text: str) -> str:
    """The reason ``read_number`` refused: "leer", "spanne", "datum" or "unlesbar"."""
    cleaned = _PREFIX.sub("", text.strip())
    if not cleaned:
        return "leer"
    if _RANGE.search(cleaned):
        return "spanne"
    if _DATE.fullmatch(cleaned):
        return "datum"
    return "unlesbar"


def format_number(value: float) -> str:
    """German formatting: thousands with a dot, up to two decimals with a comma."""
    whole, fraction = f"{value:,.2f}".split(".")
    whole = whole.replace(",", ".")
    fraction = fraction.rstrip("0")
    return f"{whole},{fraction}" if fraction else whole
