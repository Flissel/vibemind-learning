"""Gruppenflaeche (Teil C): Mitgliedschaft aus LearnHouse, Ausweis und Dienstaufrufe an den Abgleichdienst.

Der Abgleichdienst nimmt eine Verbindung nur mit einem Ausweis an, den die Huelle ausstellt; nur sie kennt
den Schluessel (`LEARNING_HUELLE_ABGLEICH_SCHLUESSEL`). Format wortgleich mit `lernflaeche/abgleich/ausweis.mjs`
(Pruefvektor `tests/huelle/ausweis_vektor.json`):

    nutzlast = "<flaeche>|<person>|<ablauf_unix>|<b64url(name_utf8)>"
    ausweis  = b64url(nutzlast) + "." + hmac_sha256_hex(sha256("abgleich-ausweis:" + schluessel), b64url(nutzlast))[:48]

Die Mitgliedschaft fragt die Huelle bei LearnHouse ueber die Dienstroute
`GET /api/v1/learning/gruppen/{gruppe_id}/personen/{actor_id}` (Dienstschluessel im Kopf, 60 s Puffer).

Geloggt werden nur Typen und Statuscodes, nie Kennungen, Namen, Adressen, Ausweise oder Schluessel.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .katalog import UebersichtWeg

log = logging.getLogger(__name__)

PUFFER_S = 60
TIMEOUT_S = 5
# Gueltigkeit eines Ausweises: nur fuer den Verbindungsaufbau (Lernflaeche -> Dienst), danach zaehlt er nicht mehr.
AUSWEIS_GUELTIG_S = 120
_SERVICE_HEADER = "X-LearnHouse-Learning-Service-Key"
DIENST_HEADER = "X-Abgleich-Dienst"
MAX_GRUPPE_ID = 999_999_999  # 1 bis 9 Ziffern wie ^g-[0-9]{1,9}$ des Dienstes

# Texte fuer Lernende (Plan Teil C, Task 7), woertlich.
KEIN_MITGLIED = "Sie gehören nicht zu dieser Gruppe. Öffnen Sie die Kursübersicht."
GRUPPE_WEG = "Die Gruppenfläche ist gerade nicht erreichbar. Versuchen Sie es in einer Minute noch einmal."
GRUPPE_VOLL = "Diese Gruppenfläche ist voll. Bitte wenden Sie sich an Ihre Kursleitung."
ZUGANG_WEG = "Ihr Zugang zu dieser Gruppe besteht nicht mehr."

# Nur die kanonische Schreibweise (keine fuehrenden Nullen): so gehoert zu jeder Gruppe genau eine Flaeche.
_FLAECHE = re.compile(r"g-(0|[1-9][0-9]{0,8})")


class KeinMitglied(Exception):
    """Die Person gehoert nicht (mehr) zur Gruppe (Dienstroute 404 oder `mitglied: false`)."""


class GruppeWeg(Exception):
    """Der Abgleichdienst antwortet nicht brauchbar (Netz, 5xx, unerwarteter Status, kaputte Antwort)."""


@dataclass(frozen=True)
class Mitgliedschaft:
    course_id: str
    gruppe_name: str
    anzeigename: str
    offen: bool


def person_kennung(actor: str) -> str:
    """Kennung der Person beim Abgleichdienst (passt auf ^p[0-9a-f]{24}$); der Actor selbst verlaesst die Huelle nie."""
    return "p" + hashlib.sha256(actor.encode("utf-8")).hexdigest()[:24]


def flaeche_der_gruppe(gruppe_id: int) -> str:
    return f"g-{gruppe_id}"


def gruppe_aus_flaeche(flaeche: str) -> int | None:
    treffer = _FLAECHE.fullmatch(flaeche) if isinstance(flaeche, str) else None
    return int(treffer.group(1)) if treffer else None


def _b64url(daten: bytes) -> str:
    return base64.urlsafe_b64encode(daten).decode("ascii").rstrip("=")


def ausweis(schluessel: str, flaeche: str, person: str, name: str, ablauf: int) -> str:
    nutzlast = f"{flaeche}|{person}|{ablauf}|{_b64url(name.encode('utf-8'))}"
    teil = _b64url(nutzlast.encode("utf-8"))
    k = hashlib.sha256(f"abgleich-ausweis:{schluessel}".encode()).digest()
    return f"{teil}.{hmac.new(k, teil.encode('ascii'), hashlib.sha256).hexdigest()[:48]}"


def dienst_kopf(schluessel: str) -> str:
    """Wert des Kopfs `X-Abgleich-Dienst`: SHA-256 von `abgleich-dienst:<schluessel>`, hex."""
    return hashlib.sha256(f"abgleich-dienst:{schluessel}".encode()).hexdigest()


def _gueltige_gruppe(gruppe_id: Any) -> bool:
    return type(gruppe_id) is int and 0 < gruppe_id <= MAX_GRUPPE_ID


class Gruppen:
    """Mitgliedschaft je (Actor, Gruppe) aus LearnHouse, 60 s gepuffert (nur positive Antworten).

    `frisch=True` fragt am Puffer vorbei (Nachpruefung offener Verbindungen). `gemerkt` liefert die letzte
    bestaetigte Mitgliedschaft, egal wie alt: bei einem LearnHouse-Ausfall darf eine bekannte Person weiterarbeiten.
    """

    def __init__(
        self, basis: str, service_key: str, http: httpx.Client | None = None,
        uhr: Callable[[], float] = time.monotonic,
    ) -> None:
        self._basis = basis.strip().rstrip("/")
        self._key = service_key.strip()
        self._http = http or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
        self._uhr = uhr
        self._sperre = threading.Lock()
        self._puffer: dict[tuple[str, int], tuple[float, Mitgliedschaft]] = {}

    def mitglied(self, actor: str, gruppe_id: int, *, frisch: bool = False) -> Mitgliedschaft:
        if not _gueltige_gruppe(gruppe_id) or not isinstance(actor, str) or not actor:
            raise KeinMitglied()
        schluessel = (actor, gruppe_id)
        jetzt = self._uhr()
        if not frisch:
            with self._sperre:
                gepuffert = self._puffer.get(schluessel)
            if gepuffert is not None and jetzt - gepuffert[0] < PUFFER_S:
                return gepuffert[1]
        try:
            m = self._abfragen(actor, gruppe_id)
        except KeinMitglied:
            with self._sperre:
                self._puffer.pop(schluessel, None)
            raise
        with self._sperre:
            self._puffer[schluessel] = (jetzt, m)
        return m

    def gemerkt(self, actor: str, gruppe_id: int) -> Mitgliedschaft | None:
        with self._sperre:
            gepuffert = self._puffer.get((actor, gruppe_id))
        return gepuffert[1] if gepuffert is not None else None

    def _abfragen(self, actor: str, gruppe_id: int) -> Mitgliedschaft:
        url = f"{self._basis}/api/v1/learning/gruppen/{gruppe_id}/personen/{quote(actor, safe='')}"
        try:
            antwort = self._http.get(url, headers={_SERVICE_HEADER: self._key}, timeout=TIMEOUT_S)
        except httpx.HTTPError as fehler:
            log.warning("Gruppen-Mitgliedschaft nicht erreichbar: %s", type(fehler).__name__)
            raise UebersichtWeg() from None
        if antwort.status_code == 404:
            raise KeinMitglied()
        if antwort.status_code != 200:
            log.warning("Gruppen-Mitgliedschaft: HTTP %s", antwort.status_code)
            raise UebersichtWeg()
        try:
            daten = antwort.json()
            if not isinstance(daten, dict):
                raise TypeError("antwort")
            mitglied, offen = daten["mitglied"], daten["offen"]
            name, anzeigename = daten["gruppe_name"], daten["anzeigename"]
            if not isinstance(mitglied, bool) or not isinstance(offen, bool) or not isinstance(name, str):
                raise TypeError("felder")
            if anzeigename is not None and not isinstance(anzeigename, str):
                raise TypeError("anzeigename")
            course_id = str(uuid.UUID(str(daten["course_id"])))
        except (ValueError, KeyError, TypeError) as fehler:
            log.warning("Gruppen-Mitgliedschaft unlesbar: %s", type(fehler).__name__)
            raise UebersichtWeg() from None
        if not mitglied:
            raise KeinMitglied()
        return Mitgliedschaft(course_id=course_id, gruppe_name=name, anzeigename=anzeigename or "", offen=offen)


class AbgleichDienst:
    """HTTP-Routen des Abgleichdienstes (Platz der Mappe, Schutzfelder), mit dem Dienstkopf."""

    def __init__(self, basis: str, schluessel: str, http: httpx.Client | None = None) -> None:
        self._basis = basis.strip().rstrip("/")
        self._kopf = {DIENST_HEADER: dienst_kopf(schluessel.strip())}
        self._http = http or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)

    def _anfrage(self, methode: str, pfad: str, daten: dict[str, Any]) -> httpx.Response:
        try:
            return self._http.request(methode, self._basis + pfad, json=daten, headers=self._kopf, timeout=TIMEOUT_S)
        except httpx.HTTPError as fehler:
            log.warning("Abgleichdienst nicht erreichbar: %s", type(fehler).__name__)
            raise GruppeWeg() from None

    def platz(self, flaeche: str, person: str) -> int:
        antwort = self._anfrage("POST", "/platz", {"flaeche": flaeche, "person": person})
        if antwort.status_code != 200:
            log.warning("Abgleichdienst /platz: HTTP %s", antwort.status_code)
            raise GruppeWeg()
        try:
            platz = antwort.json()["platz"]
        except (ValueError, KeyError, TypeError) as fehler:
            log.warning("Abgleichdienst /platz unlesbar: %s", type(fehler).__name__)
            raise GruppeWeg() from None
        if type(platz) is not int or platz < 0:
            log.warning("Abgleichdienst /platz ohne gueltigen Platz")
            raise GruppeWeg()
        return platz

    def schutz_setzen(self, flaeche: str, person: str, felder: list[list[int]]) -> None:
        antwort = self._anfrage("PUT", "/schutz", {"flaeche": flaeche, "person": person, "felder": felder})
        if antwort.status_code not in (200, 204):
            log.warning("Abgleichdienst /schutz: HTTP %s", antwort.status_code)
            raise GruppeWeg()
