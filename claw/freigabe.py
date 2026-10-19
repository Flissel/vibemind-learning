"""One-time publication tokens: issued on display, redeemed on publish."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable


class FreigabeAbgelehnt(Exception):
    """A publication token was refused; ``grund`` is a German reason."""

    def __init__(self, grund: str) -> None:
        super().__init__(grund)
        self.grund = grund


class FreigabeStore:
    """In-memory token store bound to (job, revision), single use, with a TTL."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, ttl_seconds: float = 600
    ) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._tokens: dict[str, tuple[str, int, float]] = {}
        self._lock = threading.Lock()

    def ausstellen(self, job_id: str, revision: int) -> str:
        token = "freigabe-" + secrets.token_hex(8)
        with self._lock:
            now = self._clock()
            self._tokens = {t: v for t, v in self._tokens.items() if v[2] > now}
            self._tokens[token] = (job_id, revision, now + self._ttl)
        return token

    def einloesen(self, job_id: str, revision: int, token: str) -> None:
        """Consume ``token`` (it is gone afterwards, whatever the outcome)."""
        with self._lock:
            entry = self._tokens.pop(token, None)
            now = self._clock()
        if entry is None:
            raise FreigabeAbgelehnt("Freigabe unbekannt oder bereits verwendet")
        bound_job, bound_revision, expires = entry
        if now >= expires:
            raise FreigabeAbgelehnt("Freigabe abgelaufen, bitte erneut anzeigen")
        if bound_job != job_id:
            raise FreigabeAbgelehnt("Freigabe gehoert zu einem anderen Auftrag")
        if bound_revision != revision:
            raise FreigabeAbgelehnt("Der Stand hat sich geaendert, bitte erneut anzeigen")
