"""Deterministic labels for the handwriting reader and the canvas.

The learner writes labels (A, B, C ... / 1, 2, 3 ...) on the canvas; this module maps
them back to the ids of the answer formats in ``services/evaluation/deterministic.py``.
The same ``beschriften`` call renders the labels and reads them, so the order is the
displayed order and never anything else.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

_BUCHSTABEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_MAX_TEXT = 2_000
_ZAHL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


@dataclass(frozen=True)
class Beschriftung:
    optionen: dict[str, str] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    rechts: dict[str, str] = field(default_factory=dict)


def _ids(entries: Sequence[object]) -> list[str]:
    result: list[str] = []
    for entry in entries:
        value = entry.get("id") if isinstance(entry, Mapping) else entry
        if not isinstance(value, str) or not value:
            raise ValueError("labelled entry has no id")
        result.append(value)
    return result


def _buchstaben(ids: Sequence[str]) -> dict[str, str]:
    if len(ids) > len(_BUCHSTABEN):
        raise ValueError("too many entries to label (at most 26)")
    return {_BUCHSTABEN[index]: value for index, value in enumerate(ids)}


def beschriften(
    task_type: str,
    presentation: dict[str, object],
    options: Sequence[dict[str, object]],
) -> Beschriftung:
    """Labels in displayed order: options and the right side A, B, C ..., the left side 1, 2, 3 ..."""
    optionen = _buchstaben(_ids(options))
    links: dict[str, str] = {}
    rechts: dict[str, str] = {}
    if task_type == "matching":
        left, right = presentation.get("left"), presentation.get("right")
        if isinstance(left, list):
            links = {str(index): value for index, value in enumerate(_ids(left), start=1)}
        if isinstance(right, list):
            rechts = _buchstaben(_ids(right))
    return Beschriftung(optionen=optionen, links=links, rechts=rechts)


def _etiketten(value: object) -> list[str] | None:
    """A non-empty list of distinct labels, else None. A lone label counts as a list of one."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        return None
    labels = [item.strip().upper() if isinstance(item, str) else None for item in value]
    if any(not item for item in labels) or len(set(labels)) != len(labels):
        return None
    return [item for item in labels if item]


def _aufloesen(labels: list[str] | None, table: Mapping[str, str]) -> list[str] | None:
    if labels is None or any(label not in table for label in labels):
        return None
    return [table[label] for label in labels]


def _zahl(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        text = re.sub(r"\s+", "", value.replace("−", "-").replace("–", "-").replace("—", "-"))
        if "," in text and "." in text:
            # The last separator is the decimal one, the other groups thousands.
            decimal = "," if text.rfind(",") > text.rfind(".") else "."
            group = "." if decimal == "," else ","
            text = text.replace(group, "").replace(decimal, ".")
        elif text.count(",") == 1:
            text = text.replace(",", ".")
        if not _ZAHL.match(text):
            return None
        number = float(text)
    else:
        return None
    return number if math.isfinite(number) else None


def antwort_aus_etiketten(
    task_type: str, beschriftung: Beschriftung, roh: dict[str, object]
) -> dict[str, object] | None:
    """The answer in the deterministic format, or None when nothing certain was read."""
    if task_type == "single_choice":
        ids = _aufloesen(_etiketten(roh.get("auswahl")), beschriftung.optionen)
        return {"choice_id": ids[0]} if ids is not None and len(ids) == 1 else None
    if task_type == "multiple_choice":
        ids = _aufloesen(_etiketten(roh.get("auswahl")), beschriftung.optionen)
        return {"choice_ids": ids} if ids is not None else None
    if task_type == "ordering":
        ids = _aufloesen(_etiketten(roh.get("reihenfolge")), beschriftung.optionen)
        return {"order": ids} if ids is not None else None
    if task_type == "matching":
        paare = roh.get("paare")
        if not isinstance(paare, dict) or not paare:
            return None
        pairs: dict[str, str] = {}
        for key, label in paare.items():
            if not isinstance(label, str):
                return None
            left = beschriftung.links.get(str(key).strip())
            right = beschriftung.rechts.get(label.strip().upper())
            if left is None or right is None or left in pairs:
                return None
            pairs[left] = right
        return {"pairs": pairs}
    if task_type == "numeric":
        number = _zahl(roh.get("wert"))
        return {"value": number} if number is not None else None
    if task_type == "fill_in":
        text = roh.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT:
            return None
        return {"text": text.strip()}
    return None
