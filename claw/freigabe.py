"""Publication approvals: decided by the person on a local page, never by the model.

``veroeffentlichung_anzeigen`` records the shown job, its factory revision, the course
revision and the outline, under a one-time random secret. The person opens the link
``/freigabe/<secret>`` in a browser on this machine and presses the button; only that
request publishes. The model gets the link to pass on but has no tool that publishes,
and the page refuses cross-site posts.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class FreigabeAbgelehnt(Exception):
    """An approval was refused; ``grund`` is a German reason."""

    def __init__(self, grund: str) -> None:
        super().__init__(grund)
        self.grund = grund


@dataclass(frozen=True)
class Anzeige:
    job_id: str
    revision: int
    kurs_revision: int
    ausgestellt: float
    geheimnis: str
    inhalt: dict[str, Any] = field(default_factory=dict)

    @property
    def approval_ref(self) -> str:
        """SafeToken for the publish endpoint, derived from this display record."""
        material = "|".join(
            (self.job_id, str(self.revision), str(self.kurs_revision),
             repr(self.ausgestellt), self.geheimnis)
        )
        return "freigabe-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class FreigabeStore:
    """In-memory display records with a TTL; each secret can be redeemed once."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, ttl_seconds: float = 600
    ) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._anzeigen: dict[str, Anzeige] = {}
        self._lock = threading.Lock()

    def merken(
        self, job_id: str, revision: int, kurs_revision: int, inhalt: dict[str, Any]
    ) -> Anzeige:
        with self._lock:
            now = self._clock()
            self._aufraeumen(now)
            anzeige = Anzeige(
                job_id, revision, kurs_revision, now, secrets.token_urlsafe(24), dict(inhalt)
            )
            self._anzeigen[anzeige.geheimnis] = anzeige
        return anzeige

    def holen(self, geheimnis: str) -> Anzeige:
        """Look at a pending approval without using it up."""
        with self._lock:
            self._aufraeumen(self._clock())
            anzeige = self._anzeigen.get(geheimnis)
        if anzeige is None:
            raise FreigabeAbgelehnt("Diese Freigabe gibt es nicht oder sie ist abgelaufen")
        return anzeige

    def einloesen(self, geheimnis: str) -> Anzeige:
        """Use the approval up; a second press, or one after the TTL, is refused."""
        with self._lock:
            self._aufraeumen(self._clock())
            anzeige = self._anzeigen.pop(geheimnis, None)
        if anzeige is None:
            raise FreigabeAbgelehnt("Diese Freigabe gibt es nicht oder sie ist abgelaufen")
        return anzeige

    def _aufraeumen(self, now: float) -> None:
        self._anzeigen = {
            key: value for key, value in self._anzeigen.items() if now - value.ausgestellt < self._ttl
        }
