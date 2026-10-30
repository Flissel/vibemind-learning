"""Shared rules for which source chunks are worth citing to a learner."""

from __future__ import annotations

import json
from typing import Any

MAX_BELEGE = 4


def _normal(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def nur_ueberschrift(content: object, heading: object) -> bool:
    """True if a chunk's text is empty or just its own heading (optionally with Markdown #)."""
    text = _normal(str(content or "").strip().lstrip("#"))
    return not text or text == _normal(heading)


def belege_auswaehlen(refs: list[str], limit: int = MAX_BELEGE) -> list[str]:
    """Evidence for learners: no heading-only chunks, one chunk per heading (first wins),
    first-appearance order, at most ``limit``. If nothing survives, the first ref is kept
    so an item that had evidence never ends up with none."""
    out: list[str] = []
    headings: set[str] = set()
    for ref in refs:
        try:
            data = json.loads(ref)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        locator: Any = data.get("locator")
        heading = locator.get("heading") if isinstance(locator, dict) else None
        if nur_ueberschrift(data.get("content"), heading):
            continue
        key = _normal(heading)
        if key and key in headings:
            continue
        headings.add(key)
        out.append(ref)
        if len(out) >= limit:
            break
    return out or refs[:1]
