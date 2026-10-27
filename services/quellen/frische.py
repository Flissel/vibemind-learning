"""Has a source page changed since the course was built from it?

A watch remembers the digest of a page's *visible text* (scripts, styles and whitespace
left out), so banners, tracking snippets and reformatting do not count as changes; a
changed command or sentence does. ``check`` is pure: it takes a fetcher and reports.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser

_SKIP = {"script", "style", "noscript", "template"}


@dataclass(frozen=True)
class Watch:
    id: str
    course_id: str
    url: str
    sha256: str


@dataclass(frozen=True)
class Change:
    course_id: str
    url: str
    old_sha: str
    new_sha: str


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def visible_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def text_digest(html: str) -> str:
    return hashlib.sha256(visible_text(html).encode("utf-8")).hexdigest()


def check(watches: list[Watch], fetch: Callable[[str], str]) -> list[Change]:
    """Changes among the watches; a page that cannot be fetched is skipped, not reported."""
    changes: list[Change] = []
    for watch in watches:
        try:
            html = fetch(watch.url)
        except Exception:  # noqa: BLE001 - one unreachable page must not stop the run
            continue
        new_sha = text_digest(html)
        if new_sha != watch.sha256:
            changes.append(Change(course_id=watch.course_id, url=watch.url, old_sha=watch.sha256, new_sha=new_sha))
    return changes
