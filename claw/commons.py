"""Bilder bei Wikimedia Commons finden und holen (nur lesend).

Titel, Urheber und Lizenz kommen immer aus den Angaben von Commons, nie aus einem Modell. Wikimedia verlangt,
dass sich ein automatischer Abrufer mit Namen und einer Kontaktangabe meldet; die Angabe steht in
`LEARNING_COMMONS_KONTAKT` (Webadresse oder E-Mail-Adresse des Betreibers) und nie im Code.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

import httpx

KONTAKT_ENV = "LEARNING_COMMONS_KONTAKT"
API = "https://commons.wikimedia.org/w/api.php"
_BILD_HOST = "upload.wikimedia.org"
BREITEN = (1000, 700, 500)  # px; die naechstkleinere, wenn die Datei zu gross ist
# PenEcho nimmt ein Bild als Data-URL bis 800.000 Bytes an; Base64 braucht vier Drittel.
MAX_BYTES = 580_000
_BILDER = frozenset({"image/png", "image/jpeg", "image/svg+xml"})
_FELDER = "LicenseShortName|LicenseUrl|Artist"
_TAG = re.compile(r"<[^>]*>")
_SEITE = re.compile(r"^https://commons\.wikimedia\.org/wiki/File:[^\s]{1,400}$")
_HTTPS = re.compile(r"^https://[^\s]{1,400}$")


class CommonsFehler(Exception):
    """Commons antwortet nicht brauchbar; der Text ist fuer die Person gedacht."""


@dataclass(frozen=True)
class Datei:
    datei: str  # "File:Chromosome.svg"
    titel: str  # ohne "File:"
    urheber: str  # leer, wenn Commons keinen nennt
    lizenz: str  # Kurzname wie "CC BY-SA 4.0"; leer, wenn Commons keinen nennt
    lizenz_url: str | None
    seite: str  # Dateiseite bei Commons
    breite: int
    hoehe: int
    mime: str
    bild_url: str  # verkleinerte Fassung (PNG oder JPEG)
    bild_breite: int
    bild_hoehe: int


def agent(kontakt: str) -> str:
    return f"VibeMindLearning/1.0 ({kontakt})"


def klartext(wert: Any) -> str:
    """Commons liefert Urheber als HTML: Tags weg, Zeichen aufgeloest, Leerraum zusammengezogen."""
    if not isinstance(wert, str):
        return ""
    return " ".join(html.unescape(_TAG.sub("", wert)).split())[:300]


def bildart(daten: bytes) -> str | None:
    """Dateiendung nach den ersten Bytes: nur PNG und JPEG."""
    if daten.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if daten.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def _datei(seite: dict[str, Any]) -> Datei | None:
    info = (seite.get("imageinfo") or [None])[0]
    titel = seite.get("title")
    if not isinstance(info, dict) or not isinstance(titel, str) or not titel.startswith("File:"):
        return None
    meta = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}

    def feld(name: str) -> str:
        eintrag = meta.get(name)
        return klartext(eintrag.get("value")) if isinstance(eintrag, dict) else ""

    lizenz_url = feld("LicenseUrl")
    adresse, bild_url = info.get("descriptionurl"), info.get("thumburl")
    if not isinstance(adresse, str) or not _SEITE.fullmatch(adresse) or not isinstance(bild_url, str):
        return None
    try:
        masse = [int(info[k]) for k in ("width", "height", "thumbwidth", "thumbheight")]
    except (KeyError, TypeError, ValueError):
        return None
    if min(masse) < 1:
        return None
    return Datei(
        datei=titel, titel=titel.removeprefix("File:").replace("_", " ")[:300], urheber=feld("Artist"),
        lizenz=feld("LicenseShortName")[:60], lizenz_url=lizenz_url if _HTTPS.fullmatch(lizenz_url) else None,
        seite=adresse, breite=masse[0], hoehe=masse[1], mime=str(info.get("mime") or ""),
        bild_url=bild_url, bild_breite=masse[2], bild_hoehe=masse[3],
    )


def _abfragen(http: httpx.Client, params: dict[str, str], breite: int) -> list[Datei]:
    try:
        antwort = http.get(API, params={
            "action": "query", "format": "json", "formatversion": "2", "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": str(breite), "iiextmetadatafilter": _FELDER, **params,
        })
    except httpx.HTTPError as fehler:
        raise CommonsFehler("Wikimedia Commons ist nicht erreichbar") from fehler
    if antwort.status_code != 200:
        raise CommonsFehler(
            f"Wikimedia Commons ist nicht erreichbar oder lehnt die Anfrage ab ({antwort.status_code})")
    try:
        seiten = antwort.json().get("query", {}).get("pages", [])
    except ValueError as fehler:
        raise CommonsFehler("Die Antwort von Wikimedia Commons ist nicht lesbar") from fehler
    seiten = sorted((s for s in seiten if isinstance(s, dict)), key=lambda s: s.get("index", 0))
    return [d for d in map(_datei, seiten) if d is not None]


def suchen(http: httpx.Client, suchwort: str, *, anzahl: int = 20) -> list[Datei]:
    """Dateien zum Suchwort in der Reihenfolge der Suche; ob sie brauchbar sind, entscheidet der Aufrufer."""
    return _abfragen(http, {"generator": "search", "gsrsearch": suchwort, "gsrnamespace": "6",
                            "gsrlimit": str(max(1, min(anzahl, 50)))}, BREITEN[0])


def datei(http: httpx.Client, titel: str, *, breite: int = BREITEN[0]) -> Datei | None:
    """Die Angaben zu genau einer Datei ("File:…"); None, wenn es sie nicht gibt."""
    treffer = _abfragen(http, {"titles": titel}, breite)
    return treffer[0] if treffer else None


def ist_bild(d: Datei) -> bool:
    return d.mime in _BILDER


def laden(http: httpx.Client, d: Datei) -> tuple[bytes, str] | None:
    """Die verkleinerte Fassung holen: `(Bytes, Endung)`; None, wenn sie zu gross oder kein PNG/JPEG ist.

    Abgerufen wird nur von upload.wikimedia.org."""
    try:
        adresse = httpx.URL(d.bild_url)
    except (httpx.InvalidURL, TypeError) as fehler:
        raise CommonsFehler("Die Bildadresse von Wikimedia Commons ist ungültig") from fehler
    if adresse.scheme != "https" or adresse.host != _BILD_HOST:
        raise CommonsFehler("Die Bildadresse gehört nicht zu Wikimedia Commons")
    try:
        antwort = http.get(adresse)
    except httpx.HTTPError as fehler:
        raise CommonsFehler("Das Bild ließ sich nicht von Wikimedia Commons holen") from fehler
    if antwort.status_code != 200:
        raise CommonsFehler(f"Das Bild ließ sich nicht von Wikimedia Commons holen ({antwort.status_code})")
    daten = antwort.content
    art = bildart(daten)
    if art is None or len(daten) > MAX_BYTES:
        return None
    return daten, art
