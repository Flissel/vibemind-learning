"""Small text helpers shared by the sequence builders."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[^A-Za-z0-9._-]+")


def token(value: str, limit: int = 60) -> str:
    """A safe identifier made from free text (``SafeToken`` compatible)."""
    cleaned = _TOKEN.sub("-", value).strip("-.") or "x"
    return cleaned[:limit]


def plain(text: str) -> str:
    """Markdown emphasis and code marks removed."""
    return re.sub(r"(\*\*|__|`)", "", text).strip()


def cut(text: str, limit: int) -> str:
    """Whitespace collapsed and cut at a word boundary with an ellipsis."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"
