"""Client fuer den Bewertungsendpunkt der Lernflaeche (Teil 2 des KI-Tutors).

Die Huelle schickt Schnappschuss und Bewertungsdaten von Server zu Server; der Browser sieht nichts davon.
Die Bewertungsdaten enthalten die Musterloesung und die Rubrik: geloggt werden darum nur Typen und
Statuscodes, nie Inhalte und nie der Schluessel. Jeder Fehler ergibt None, dann bewertet der Server.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

PFAD = "/api/lernflaeche/bewerten"
KOPF = "X-Lernflaeche-Bewertung"
TIMEOUT_S = 120.0
GRADER = "ki_flaeche"


class KiBewerter:
    def __init__(
        self,
        url: str,
        schluessel: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self._url = url.strip().rstrip("/")
        self._schluessel = schluessel.strip()
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)

    @property
    def aktiv(self) -> bool:
        """Ohne Schluessel oder Adresse gibt es keinen Aufruf (die Huelle bewertet dann wie bisher)."""
        return bool(self._url and self._schluessel)

    def bewerten(self, bild_png: bytes, daten: dict[str, Any]) -> dict[str, Any] | None:
        """Das fertige `externe_bewertung`-Objekt oder None (kein Schluessel, Fehler, Zeit, unbrauchbare Antwort)."""
        if not self.aktiv:
            return None
        body = {"bild_png_base64": base64.b64encode(bild_png).decode("ascii"), "daten": daten}
        try:
            antwort = self._client.post(self._url + PFAD, json=body, headers={KOPF: self._schluessel})
        except httpx.HTTPError as fehler:
            log.warning("KI-Bewertung nicht erreichbar: %s", type(fehler).__name__)
            return None
        if antwort.status_code != 200:
            log.warning("KI-Bewertung: HTTP %s", antwort.status_code)
            return None
        try:
            wert = antwort.json()
        except ValueError as fehler:
            log.warning("KI-Bewertung ohne lesbare Antwort: %s", type(fehler).__name__)
            return None
        if not isinstance(wert, dict) or wert.get("grader") != GRADER:
            log.warning("KI-Bewertung unbrauchbar")
            return None
        return wert
