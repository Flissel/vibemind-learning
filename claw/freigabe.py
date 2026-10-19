"""Publication displays: recorded on display, redeemed once on publish (spec 9.1).

No token reaches the model. ``veroeffentlichung_anzeigen`` records which (job, factory
revision, course revision) it showed, in which MCP session and when. ``veroeffentlichen``
names the two revisions it saw in the summary; it is accepted only if a matching display
exists that is younger than the TTL and came from a DIFFERENT, earlier MCP session. The
shim starts a fresh ``claude -p`` (and so a fresh MCP session) per chat turn, so a
different session means a person turn lay between display and publish.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


class FreigabeAbgelehnt(Exception):
    """A publication was refused; ``grund`` is a German reason."""

    def __init__(self, grund: str) -> None:
        super().__init__(grund)
        self.grund = grund


@dataclass(frozen=True)
class Anzeige:
    job_id: str
    revision: int
    kurs_revision: int
    sitzung: str
    ausgestellt: float
    nonce: str

    @property
    def approval_ref(self) -> str:
        """SafeToken for the publish endpoint, derived from this display record."""
        material = "|".join(
            (self.job_id, str(self.revision), str(self.kurs_revision), self.sitzung,
             repr(self.ausgestellt), self.nonce)
        )
        return "freigabe-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class FreigabeStore:
    """In-memory display records with a TTL, consumed on use."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, ttl_seconds: float = 600
    ) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._anzeigen: list[Anzeige] = []
        self._lock = threading.Lock()

    def merken(self, job_id: str, revision: int, kurs_revision: int, sitzung: str) -> Anzeige:
        with self._lock:
            now = self._clock()
            self._anzeigen = [a for a in self._anzeigen if now - a.ausgestellt < self._ttl]
            anzeige = Anzeige(job_id, revision, kurs_revision, sitzung, now, secrets.token_hex(8))
            self._anzeigen.append(anzeige)
        return anzeige

    def einloesen(self, job_id: str, revision: int, kurs_revision: int, sitzung: str) -> Anzeige:
        """Consume the matching display. A refusal for "same session" leaves it in place."""
        if not sitzung:
            raise FreigabeAbgelehnt(
                "Keine MCP-Sitzungskennung - Veroeffentlichung abgelehnt"
            )
        with self._lock:
            now = self._clock()
            zum_auftrag = [a for a in self._anzeigen if a.job_id == job_id]
            frisch = [a for a in zum_auftrag if now - a.ausgestellt < self._ttl]
            self._anzeigen = [a for a in self._anzeigen if now - a.ausgestellt < self._ttl]
            if not zum_auftrag:
                raise FreigabeAbgelehnt(
                    "Keine Anzeige zu diesem Auftrag - zuerst veroeffentlichung_anzeigen"
                )
            if not frisch:
                raise FreigabeAbgelehnt("Anzeige abgelaufen, bitte erneut anzeigen")
            passend = [
                a for a in frisch if a.revision == revision and a.kurs_revision == kurs_revision
            ]
            if not passend:
                raise FreigabeAbgelehnt(
                    "Die Revisionen passen zu keiner Anzeige, bitte erneut anzeigen"
                )
            frueher = [a for a in passend if a.sitzung != sitzung and a.ausgestellt < now]
            if not frueher:
                raise FreigabeAbgelehnt(
                    "Das Ja muss in einer spaeteren Nachricht nach der Anzeige kommen"
                )
            gewaehlt = max(frueher, key=lambda a: a.ausgestellt)
            self._anzeigen = [a for a in self._anzeigen if a not in passend]
        return gewaehlt
