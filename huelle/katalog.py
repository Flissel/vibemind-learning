"""Kurse je Person: welche Kurse eine Kennung auf der Lern-Huelle oeffnen darf.

Die Inhaber-Kennung des Notwegs (Telegram-Zugang, `LEARNING_HUELLE_ACTOR`) bekommt die Kurse aus LearnHouse
(ihr eigenes Konto ist mit dieser Kennung verknuepft, die Kursuebersicht zeigt also alle Kurse) plus die
Kursliste der Umgebung (`LEARNING_HUELLE_KURSE`). Antwortet LearnHouse nicht brauchbar, bleibt fuer sie nur
die Liste der Umgebung: der Notweg haengt nie an LearnHouse. Jede andere Kennung fragt LearnHouse ueber die Dienstroute
`GET /api/v1/learning/personen/{actor_id}/kurse` (Dienstschluessel im Kopf), mit 60 s Puffer je Kennung:
eine entfernte Person verliert den Zugang also spaetestens 60 s nach dem Entfernen. Ohne
`LEARNING_HUELLE_LEARNHOUSE_URL` gibt es nur den Notweg.

Geloggt werden nur Typen und Statuscodes, nie Kennungen, Kurse, Adressen oder Schluessel.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from urllib.parse import quote

import httpx

from .einstellung import Einstellung, Kurs

log = logging.getLogger(__name__)

PUFFER_S = 60
TIMEOUT_S = 5
_SERVICE_HEADER = "X-LearnHouse-Learning-Service-Key"
# Texte fuer Lernende (Plan Teil B, Global Constraints), woertlich.
KEIN_ZUGANG_MEHR = "Ihr Zugang zu diesem Kurs besteht nicht mehr. Öffnen Sie die Kursübersicht."
UEBERSICHT_WEG = "Die Kursübersicht ist gerade nicht erreichbar. Bitte versuchen Sie es gleich noch einmal."


class KeinZugang(Exception):
    """Die Kennung gehoert (nicht mehr) zu einer Person mit Anbieter-Mitgliedschaft (Dienstroute 404)."""


class UebersichtWeg(Exception):
    """LearnHouse antwortet nicht brauchbar (Netz, 5xx, unerwarteter Status, kaputte Antwort)."""


class Katalog:
    def __init__(
        self,
        einstellung: Einstellung,
        http: httpx.Client | None = None,
        uhr: Callable[[], float] = time.monotonic,
    ) -> None:
        self._e = einstellung
        self._http = http or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._uhr = uhr
        self._sperre = threading.Lock()
        self._puffer: dict[str, tuple[float, tuple[Kurs, ...]]] = {}

    def kurse(self, actor_id: str) -> tuple[Kurs, ...]:
        if actor_id == self._e.actor_id:
            return self._kurse_inhaber()
        if not self._e.learnhouse_url:
            raise KeinZugang()
        return self._gepuffert(actor_id)

    def _kurse_inhaber(self) -> tuple[Kurs, ...]:
        notweg = self._e.kurse
        if not self._e.learnhouse_url:
            return notweg
        try:
            aus_learnhouse = self._gepuffert(self._e.actor_id)
        except (KeinZugang, UebersichtWeg):
            # Auch den Fehlschlag 60 s puffern: sonst bremst ein toter LearnHouse jede Notweg-Anfrage.
            with self._sperre:
                self._puffer[self._e.actor_id] = (self._uhr(), ())
            return notweg
        bekannt = {k.course_id for k in aus_learnhouse}
        return aus_learnhouse + tuple(k for k in notweg if k.course_id not in bekannt)

    def _gepuffert(self, actor_id: str) -> tuple[Kurs, ...]:
        jetzt = self._uhr()
        with self._sperre:
            gepuffert = self._puffer.get(actor_id)
        if gepuffert is not None and jetzt - gepuffert[0] < PUFFER_S:
            return gepuffert[1]
        kurse = self._abfragen(actor_id)
        with self._sperre:
            self._puffer[actor_id] = (jetzt, kurse)
        return kurse

    def _abfragen(self, actor_id: str) -> tuple[Kurs, ...]:
        url = f"{self._e.learnhouse_url}/api/v1/learning/personen/{quote(actor_id, safe='')}/kurse"
        try:
            antwort = self._http.get(url, headers={_SERVICE_HEADER: self._e.dienst_schluessel}, timeout=TIMEOUT_S)
        except httpx.HTTPError as fehler:
            log.warning("Kursuebersicht nicht erreichbar: %s", type(fehler).__name__)
            raise UebersichtWeg() from None
        if antwort.status_code == 404:
            with self._sperre:
                self._puffer.pop(actor_id, None)
            raise KeinZugang()
        if antwort.status_code != 200:
            log.warning("Kursuebersicht: HTTP %s", antwort.status_code)
            raise UebersichtWeg()
        try:
            eintraege = antwort.json()["kurse"]
            if not isinstance(eintraege, list):
                raise TypeError("kurse")
        except (ValueError, KeyError, TypeError) as fehler:
            log.warning("Kursuebersicht ohne Kursliste: %s", type(fehler).__name__)
            raise UebersichtWeg() from None
        kurse = []
        for eintrag in eintraege:
            try:
                titel = eintrag["titel"]
                if not isinstance(titel, str):
                    raise TypeError("titel")
                kurse.append(Kurs(str(uuid.UUID(str(eintrag["course_id"]))), titel))
            except (ValueError, KeyError, TypeError) as fehler:
                log.warning("Kurseintrag verworfen: %s", type(fehler).__name__)
        return tuple(kurse)
