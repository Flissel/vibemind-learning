"""MCP-Client der Huelle fuer PenEcho (JSON-RPC ueber HTTPS, gepinntes Zertifikat).

Argumentformen: node_modules/penecho/src/server/mcp/schema.js (PenEcho 1.4.2).
Der Bearer-Token wird bei jedem Verbindungsaufbau aus identity.json gelesen und
nie geloggt; Fehler werden nur als Typname gemeldet.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import ssl
import time
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

import httpx

from .anordnung import markenzeile, zeile
from .tafel import kuerzen, rueckmeldungstext

log = logging.getLogger(__name__)

PROTOKOLL = "2025-11-25"
SCHLUESSEL_PRAEFIX = "learning-huelle:"
MAX_SCHLUESSEL = 128  # schema.js: sessionKey/sessionId/requestId maxLength 128

# Ruling L neu: eine Flaeche je Lernsitzung, 20 Aufgabenbereiche untereinander im nativen Massstab
# (schema.js: Weltkoordinaten 0..20000). PenEcho setzt Text an festen Koordinaten mit 20 px und bricht
# bei 400 um (canvas-documents.js create_text), ohne zu skalieren; in 9000 breiten Bereichen war er
# winzig. Rechts neben jedem Bereich liegt sein Rueckmeldungsfeld, ausserhalb des Schnappschusses.
FLAECHE_GROESSE = 20000
BEREICHE = 20
BEREICH_BREITE = 1300
BEREICH_HOEHE = 800
_RAND = 100
_ZEILEN_SCHRITT = 850
_TEXT_RAND = 20
_TEXT_BREITE = 460
_RUECKMELDUNG_ABSTAND = 40
# A3: die Karte war mit 420 in normal hohen Bereichen nach rund 1000 Zeichen zu Ende. Die Lernflaeche bricht
# Text mit Bereich an dessen Breite um (lernflaeche-kern.js textUmbruchBreite), also fasst 760 fast doppelt so viel.
# Rechts davon bleibt das Berichtsbild links der zweiten Spalte (anordnung.SPALTEN_X).
_RUECKMELDUNG_BREITE = 760
# Abnahme 06.11. (F14): PenEcho rendert hoechstens 2000 Zeichen (core.js TEXT_INPUT_MAX_LENGTH, sonst
# INVALID_TEXT), Schrift 20 px mit Zeilenhoehe 1,35 (27 px), Umbruch bei 400 px. Ein laengerer Text ragte ueber
# das Rueckmeldungsfeld in den naechsten Bereich. 36 Zeichen je Zeile ist vorsichtig geschaetzt.
_TEXT_HOECHSTENS = 2000
_ZEILEN_HOEHE = 27
_RUECKMELDUNG_ZEILEN = (BEREICH_HOEHE - 2 * _TEXT_RAND) // _ZEILEN_HOEHE
_ZEICHEN_JE_ZEILE = 36  # je 400 px
# Im 760 breiten Kartenfeld: 36 * 760 / 400 = 68, vorsichtig 64.
_RUECKMELDUNG_ZEICHEN_JE_ZEILE = 64

# Ergebnisfeld (Task 6): oben rechts im Bereich, darunter bleibt Platz fuer Rechenweg oder Skizze.
# Der Rahmen liegt AUSSERHALB des Feldes, damit der Schnappschuss des Feldes nur die
# Handschrift der Lernenden enthaelt.
FELD_BREITE = 480
FELD_HOEHE = 200
_FELD_RAND_RECHTS = 20
_FELD_RAND_OBEN = 80
_RAHMEN_ABSTAND = 8
_RAHMEN_STAERKE = 4
_RAHMEN_FARBE = "#2b6cb0"
# "Wird noch geprüft.": Learnings Zustand needs_review (Rubrik-Bewertung nicht angenommen).
URTEILE = ("Richtig.", "Teilweise richtig.", "Noch nicht richtig.", "Nicht lesbar.", "Wird noch geprüft.")
# Abnahme 06.11. (F15): PenEcho nimmt je Flaeche hoechstens 50 Texte (canvasDocumentsCapacity "text" 50,
# MAX_VISIBLE_TEXT_BOXES 50 auch fuer die Lernenden). Ein eigenes Schild ueber dem Feld kostete je Aufgabe
# einen dritten Text; der Hinweis steht deshalb als letzte Zeile im Aufgabentext.
_HINWEIS_ERGEBNIS = "Ihr Ergebnis schreiben Sie in das blau umrandete Feld."
_HINWEIS_LOESUNG = "Ihre Lösung schreiben Sie in das blau umrandete Feld."

# Bericht am Kursende. Urheberschafts-Befund (Task 1, BOARD_SPEICHERN.md): PenEchos KI-Tinte landet in
# denselben Raster-Kacheln wie die Striche der Lernenden, nur Plots und Widgets sind trennbar. Darum
# kommt zur Berichtskarte je Aufgabe (PenEcho platziert selbst) der Schnappschuss des Bereichs. Trennt PenEcho die KI-Objekte
# einmal sauber, hier auf False stellen; kein Laufzeit-Test.
BERICHT_MIT_SCHNAPPSCHUESSEN = True
MAX_BILD_DATENURL = 800_000  # schema.js penecho_place_image/upload_image: Data-URL inkl. Praefix, UTF-8-Bytes
BERICHT_BILD_BREITE = 700  # A3: 900 ragte nach der breiteren Karte in die zweite Spalte
BERICHT_BILD_ZEIT = 20.0  # Sekunden fuer alle Berichtsbilder zusammen; danach entfallen die uebrigen
MAX_TITEL = 120  # schema.js MAX_TITLE_CHARS
_uhr = time.monotonic  # nur fuer Tests ersetzbar


def bereich(nummer: int) -> dict[str, int]:
    """Weltregion des Aufgabenbereichs `nummer` (1..20); ausserhalb ValueError."""
    if isinstance(nummer, bool) or not isinstance(nummer, int) or not 1 <= nummer <= BEREICHE:
        raise ValueError("Aufgabenbereich ausserhalb der Flaeche")
    return {"x": _RAND, "y": _RAND + (nummer - 1) * _ZEILEN_SCHRITT, "w": BEREICH_BREITE, "h": BEREICH_HOEHE}


_BERICHT_ABSTAND = 80


class Felder(NamedTuple):
    """Die festen Plaetze eines Aufgabenbereichs, alle relativ zu dessen Rechteck (siehe `felder`)."""

    textfeld: dict[str, int]
    ergebnisfeld: dict[str, int]
    arbeitsfeld: dict[str, int]
    rueckmeldungsfeld: dict[str, int]
    berichtsplatz: dict[str, int]


def felder(b: dict[str, int]) -> Felder:
    """Text, Ergebnisfeld, Arbeitsfeld, Rueckmeldung und Berichtsbild zum Bereichsrechteck `b`.

    Dieselben Abstaende wie im festen 850er-Raster (`bereich`); ein hoeherer Bereich (Listenkurs, Aufgabe
    mit Bild) verlaengert Text, Arbeitsfeld und Rueckmeldung nach unten, das Ergebnisfeld bleibt oben.
    """
    text = {"x": b["x"] + _TEXT_RAND, "y": b["y"] + _TEXT_RAND, "w": _TEXT_BREITE, "h": b["h"] - 2 * _TEXT_RAND}
    ergebnis = {
        "x": b["x"] + b["w"] - _FELD_RAND_RECHTS - FELD_BREITE,
        "y": b["y"] + _FELD_RAND_OBEN,
        "w": FELD_BREITE,
        "h": FELD_HOEHE,
    }
    x = text["x"] + text["w"] + _TEXT_RAND
    arbeit = {"x": x, "y": b["y"] + _FELD_RAND_OBEN, "w": b["x"] + b["w"] - _FELD_RAND_RECHTS - x,
              "h": b["h"] - _FELD_RAND_OBEN - _TEXT_RAND}
    rueckmeldung = {"x": b["x"] + b["w"] + _RUECKMELDUNG_ABSTAND, "y": b["y"] + _TEXT_RAND,
                    "w": _RUECKMELDUNG_BREITE, "h": b["h"] - 2 * _TEXT_RAND}
    bericht = {"x": rueckmeldung["x"] + rueckmeldung["w"] + _BERICHT_ABSTAND, "y": b["y"],
               "w": BERICHT_BILD_BREITE, "h": b["h"]}
    return Felder(text, ergebnis, arbeit, rueckmeldung, bericht)


def textfeld(nummer: int) -> dict[str, int]:
    """Platz des Aufgabentexts oben links in `bereich(nummer)`, links neben dem Ergebnisfeld."""
    return felder(bereich(nummer)).textfeld


def ergebnisfeld(nummer: int) -> dict[str, int]:
    """Weltrechteck des Ergebnisfelds oben rechts in `bereich(nummer)` (>= 360x140)."""
    return felder(bereich(nummer)).ergebnisfeld


def arbeitsfeld(nummer: int) -> dict[str, int]:
    """Platz fuer Skizze oder freie Antwort (offene/Zeichenaufgaben): rechts neben dem Text, bis unten.

    Nur eine sichtbare Grenze; bewertet wird weiter der ganze Bereich (`schnappschuss`).
    """
    return felder(bereich(nummer)).arbeitsfeld


_BILD_LUECKE = 20
_BILD_SCHILD = 40  # Hoehe der Beschriftung ueber einem Bild (create_text, Schrift 20)
_BILD_MIN = 80  # schema.js penecho_place_image: width/height minimum 80


class Aufgabenbild(NamedTuple):
    """Ein Bild der Aufgabe (Graph, Skizze) mit optionaler Beschriftung; `seitenverhaeltnis` = Breite/Hoehe."""

    beschriftung: str | None
    daten: bytes
    seitenverhaeltnis: float


def bildfeld(nummer: int, *, unter_ergebnisfeld: bool, rechteck: dict[str, int] | None = None) -> dict[str, int]:
    """Platz der Aufgabenbilder rechts neben dem Text.

    Mit Ergebnisfeld darunter, bis zum Bereichsende; sonst oben im Arbeitsfeld, damit darunter Platz zum
    Schreiben bleibt und offene Aufgaben direkt im Bild beschriftet werden koennen. `rechteck` ersetzt
    `bereich(nummer)` (Listenkurs, dann ohne die Grenze 1..20).
    """
    f = felder(rechteck if rechteck is not None else bereich(nummer))
    a = f.arbeitsfeld
    if unter_ergebnisfeld:
        e = f.ergebnisfeld
        y = e["y"] + e["h"] + 2 * _BILD_LUECKE
        return {"x": a["x"], "y": y, "w": a["w"], "h": a["y"] + a["h"] - y}
    return {"x": a["x"] + _BILD_LUECKE, "y": a["y"] + _BILD_LUECKE, "w": a["w"] - 2 * _BILD_LUECKE,
            "h": a["h"] // 2 - _BILD_LUECKE}


def bildraster(feld: dict[str, int], bilder: Sequence[Aufgabenbild]) -> list[tuple[dict[str, int] | None, dict[str, int]]]:
    """Je Bild (Schild-Region oder None, Bild-Region) im Raster: eins allein, bis vier 2x2, mehr in drei Spalten."""
    anzahl = len(bilder)
    if anzahl == 0:
        return []
    spalten = 1 if anzahl == 1 else 2 if anzahl <= 4 else 3
    zeilen = -(-anzahl // spalten)
    zelle_b = (feld["w"] - (spalten - 1) * _BILD_LUECKE) // spalten
    zelle_h = (feld["h"] - (zeilen - 1) * _BILD_LUECKE) // zeilen
    plaetze: list[tuple[dict[str, int] | None, dict[str, int]]] = []
    for i, bild in enumerate(bilder):
        x = feld["x"] + (i % spalten) * (zelle_b + _BILD_LUECKE)
        y = feld["y"] + (i // spalten) * (zelle_h + _BILD_LUECKE)
        schild = None
        if bild.beschriftung:
            schild = {"x": x, "y": y, "w": zelle_b, "h": _BILD_SCHILD}
            y += _BILD_SCHILD
        verhaeltnis = bild.seitenverhaeltnis if bild.seitenverhaeltnis > 0 else 1.0
        breite = round(min(zelle_b, (zelle_h - (_BILD_SCHILD if schild else 0)) * verhaeltnis))
        hoehe = round(breite / verhaeltnis)
        plaetze.append((schild, {"x": x, "y": y, "w": breite, "h": hoehe}))
    return plaetze


def rueckmeldungsfeld(nummer: int) -> dict[str, int]:
    """Platz der Rueckmeldung rechts neben `bereich(nummer)`, nicht im Schnappschuss des Bereichs."""
    return felder(bereich(nummer)).rueckmeldungsfeld


def berichtsplatz(nummer: int) -> dict[str, int]:
    """Platz des Berichtsbilds (Schnappschuss) zu Aufgabe `nummer`: rechts neben ihrem Rueckmeldungsfeld.

    Abnahme 06.11. (F19): mit fester region setzt PenEcho das Bild im Massstab 1; ohne region rechnete es
    die Breite durch den Zoom der Ansicht und lehnte bei kleinem Zoom ab (INVALID_GEOMETRY).
    Ein Schnappschuss des Bereichs (1300 x 800) ist bei Breite 900 rund 554 hoch und passt in die Zeile.
    """
    return felder(bereich(nummer)).berichtsplatz


def _ueberlappt(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


def _spalten(plaetze: Iterable[dict[str, int]]) -> list[dict[str, int]]:
    """Je Spalte (gleiches x und w) ein Rechteck um alle ihre Plaetze: eine Belegungsabfrage je Spalte."""
    spalten: dict[tuple[int, int], tuple[int, int]] = {}
    for p in plaetze:
        schluessel = (p["x"], p["w"])
        oben, unten = spalten.get(schluessel, (p["y"], p["y"] + p["h"]))
        spalten[schluessel] = (min(oben, p["y"]), max(unten, p["y"] + p["h"]))
    return [{"x": x, "y": oben, "w": w, "h": unten - oben} for (x, w), (oben, unten) in spalten.items()]


_KONTEXT = "context.md"
_MARKE = re.compile(r"<!-- lernflaeche-bereiche:.*-->")


def _markenpatch(inhalt: str, neu: str) -> str | None:
    """Strikter Unified Diff (PenEcho penecho_patch_file, jsdiff ohne Unschaerfe) fuer die Markenzeile `neu`.

    Ersetzt die erste vorhandene Markenzeile (eine Zeile, ohne Kontext) oder haengt `neu` an das Ende;
    dann steht die letzte Zeile als Kontext davor. None, wenn die Marke schon genau so dasteht.
    Zeilen enden mit LF; fehlt am Dateiende der Zeilenumbruch, traegt der Diff die Kennzeile dafuer.
    """
    kopf = f"--- a/{_KONTEXT}\n+++ b/{_KONTEXT}\n"
    offen_am_ende = bool(inhalt) and not inhalt.endswith("\n")
    zeilen = inhalt.split("\n")
    if not offen_am_ende:
        zeilen.pop()  # "" nach dem letzten Zeilenumbruch (oder der leere Inhalt)
    ohne_lf = "\n\\ No newline at end of file\n"
    for i, z in enumerate(zeilen):
        if _MARKE.fullmatch(z.strip()):
            if z == neu:
                return None
            ende = ohne_lf if offen_am_ende and i == len(zeilen) - 1 else "\n"
            return f"{kopf}@@ -{i + 1},1 +{i + 1},1 @@\n-{z}{ende}+{neu}{ende}"
    if not zeilen:
        return f"{kopf}@@ -0,0 +1,1 @@\n+{neu}\n"
    n, letzte = len(zeilen), zeilen[-1]
    if offen_am_ende:
        return f"{kopf}@@ -{n},1 +{n},2 @@\n-{letzte}{ohne_lf}+{letzte}\n+{neu}\n"
    return f"{kopf}@@ -{n},1 +{n},2 @@\n {letzte}\n+{neu}\n"


def _sitzungsschluessel(schluessel: str) -> str:
    roh = SCHLUESSEL_PRAEFIX + schluessel
    if len(roh) <= MAX_SCHLUESSEL:
        return roh
    return SCHLUESSEL_PRAEFIX + hashlib.sha256(schluessel.encode("utf-8")).hexdigest()


def _tab_marke(canvas_id: str) -> str:
    """Kurzer Hash der Flaechen-Kennung eines Tabs: fuer Merkdatei und sessionKey, nie die Kennung selbst."""
    return hashlib.sha256(canvas_id.encode("utf-8")).hexdigest()[:32]


class PenEchoNichtErreichbar(Exception):
    """PenEcho antwortet nicht oder lehnt die Anfrage ab."""


class PenEchoSitzungUnbekannt(PenEchoNichtErreichbar):
    """PenEcho kennt die Canvas-Sitzung (sessionId) nicht mehr oder sie ist geschlossen."""


# Fehlercodes aus PenEcho service.js (ownedSession): session_not_found, session_expired,
# session_closed, canvas_disconnected.
class PenEchoFlaecheVerloren(PenEchoNichtErreichbar):
    """Die an den sessionKey gebundene Flaeche ist nicht mehr gespeichert (z. B. nach Neustart)."""


class PenEchoPlatzBelegt(PenEchoNichtErreichbar):
    """Der gewuenschte Bereich ist schon belegt (PenEcho LAYOUT_CONFLICT).

    `hindernisse`: PenEchos details.obstacles (canvas-documents.js, je {id,x,y,w,h}), nur gueltige Eintraege.
    """

    def __init__(self, code: str, hindernisse: Sequence[dict[str, Any]] = ()) -> None:
        super().__init__(code)
        self.hindernisse = list(hindernisse)


def _eigene_texte(hindernisse: Sequence[dict[str, Any]], platz: dict[str, int]) -> list[dict[str, Any]]:
    """Texte, die exakt an der Ecke von `platz` stehen: dort setzt nur die Huelle selbst Text."""
    return [h for h in hindernisse if h["id"].startswith("text-box-")
            and abs(h["x"] - platz["x"]) < 1 and abs(h["y"] - platz["y"]) < 1]


def _an_ecke(belegung: Sequence[dict[str, Any]], platz: dict[str, int], art: str) -> str | None:
    """objectId des ersten Objekts der Art `art`, dessen linke obere Ecke auf der von `platz` liegt."""
    for e in belegung:
        b = e["bounds"]
        if e["kind"] == art and abs(b["x"] - platz["x"]) < 1 and abs(b["y"] - platz["y"]) < 1:
            return e["objectId"]
    return None


def _hindernisse(struktur: dict[str, Any]) -> list[dict[str, Any]]:
    details = struktur.get("details")
    roh = details.get("obstacles") if isinstance(details, dict) else None
    if not isinstance(roh, list):
        return []
    return [h for h in roh if isinstance(h, dict) and isinstance(h.get("id"), str)
            and all(isinstance(h.get(k), (int, float)) and not isinstance(h.get(k), bool) for k in "xywh")]


class PenEchoFlaecheVoll(PenEchoNichtErreichbar):
    """PenEchos Objektgrenze je Flaeche ist erreicht (OBJECT_LIMIT, z. B. 50 Texte)."""


class PenEchoAnfragenStau(PenEchoSitzungUnbekannt):
    """PenEcho meldet request_limit: zu viele gescheiterte Aenderungen in dieser Sitzung.

    bound-operations.js (PenEcho 1.4.2): jeder Aenderungsaufruf, der mit einem Fehler endet, bleibt
    "unaufgeloest" (MAX_UNRESOLVED_PATCHES = 4), bis dieselbe requestId mit denselben Argumenten gelingt.
    Danach nimmt die Sitzung keine neue requestId mehr an. Die Huelle schliesst sie deshalb; als
    PenEchoSitzungUnbekannt legt die Bruecke ueber denselben sessionKey eine frische an.
    """


class PenEchoRevisionVeraltet(PenEchoNichtErreichbar):
    """PenEcho meldet REVISION_CONFLICT: die Flaeche hat sich seit dem Lesen der Revision geaendert."""


class PenEchoAnfrageSchonBekannt(PenEchoNichtErreichbar):
    """PenEcho meldet REQUEST_ID_CONFLICT: die requestId wurde mit anderen Argumenten schon benutzt."""


class PenEchoNichtSichtbar(PenEchoNichtErreichbar):
    """PenEcho meldet ACTIVE_CANVAS_REQUIRED: draw_ink braucht das Board sichtbar (show)."""


class PenEchoQuelleVeraltet(PenEchoNichtErreichbar):
    """PenEcho meldet SOURCE_CONFLICT oder PATCH_CONFLICT: die Datei hat sich seit dem Lesen geaendert."""


class PenEchoSchluesselBelegt(PenEchoNichtErreichbar):
    """PenEcho meldet session_key_conflict: der sessionKey ist an ein anderes Board oder einen anderen Tab gebunden."""


_SITZUNG_CODES = frozenset({"session_not_found", "session_expired", "session_closed", "canvas_disconnected"})
# canvas-documents.js (PenEcho 1.4.2): Wiederherstellen einer nie gesicherten Flaeche.
_VERLOREN_CODES = frozenset({"STORAGE_UNAVAILABLE", "DOCUMENT_NOT_FOUND"})
_CODE_MUSTER = re.compile(r"[A-Za-z_]{1,40}")


class _HttpFehler(PenEchoNichtErreichbar):
    def __init__(self, text: str, status: int | None) -> None:
        super().__init__(text)
        self.status = status


def _typ(fehler: BaseException) -> str:
    return type(fehler).__name__


def _datenurl(daten: bytes) -> str | None:
    """Data-URL fuer penecho_place_image (PNG/JPEG/WebP); anderes Format None."""
    if daten.startswith(b"\x89PNG\r\n\x1a\n"):
        art = "png"
    elif daten.startswith(b"\xff\xd8\xff"):
        art = "jpeg"
    elif daten[:4] == b"RIFF" and daten[8:12] == b"WEBP":
        art = "webp"
    else:
        return None
    return f"data:image/{art};base64," + base64.b64encode(daten).decode("ascii")


class PenEchoMcp:
    def __init__(
        self,
        state_dir: Path,
        *,
        url: str = "https://127.0.0.1:3922/mcp",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._url = url
        self._transport = transport  # nur fuer Tests (MockTransport)
        self._timeout = timeout
        self._client: httpx.Client | None = None
        self._mcp_session: str | None = None
        self._next_id = 0
        self._merkdatei = self._state_dir.parent / "learning-huelle" / "sitzungen.json"
        # Ersatz-sessionKeys fuer Flaechen, die neu angelegt werden mussten (Schluessel -> sessionKey).
        self._ersatzdatei = self._merkdatei.with_name("flaechen.json")
        # PenEcho-Dokument-ID des Boards je Lernsitzung (Schluessel -> documentId), BOARD_SPEICHERN.md.
        self._dokumentdatei = self._merkdatei.with_name("dokumente.json")
        # Teil B: Flaechen-Kennung (canvasId des Tabs), an die die gemerkte Sitzung gebunden ist (Schluessel -> canvasId).
        self._tabdatei = self._merkdatei.with_name("tabs.json")

    # ---- Verbindung -------------------------------------------------------
    def _identity(self) -> dict[str, Any]:
        try:
            datei = self._state_dir / "direct-http" / "identity.json"
            wert = json.loads(datei.read_text(encoding="utf-8"))
            if not isinstance(wert, dict) or not isinstance(wert.get("accessToken"), str):
                raise ValueError("identity")
            return wert
        except Exception as fehler:
            log.warning("PenEcho-Identitaet nicht lesbar: %s", _typ(fehler))
            raise PenEchoNichtErreichbar("identity") from None

    def _ssl_kontext(self, identity: dict[str, Any]) -> ssl.SSLContext:
        # Pinning: nur die lokale PenEcho-CA (certificatePem) ist Vertrauensanker.
        pem = identity.get("certificatePem")
        if not isinstance(pem, str) or not pem:
            raise PenEchoNichtErreichbar("certificate")
        kontext = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cadata=pem)
        # Das Blattzertifikat von PenEcho hat weder AKI noch SKI; die strikte X509-Pruefung
        # (Python >= 3.13) wuerde es ablehnen. Kette, Ablauf und Hostname bleiben geprueft.
        kontext.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        return kontext

    def _verbinden(self) -> None:
        if self._client is not None:
            return
        try:
            identity = self._identity()
            kopf = {"Authorization": "Bearer " + identity["accessToken"]}
            if self._transport is not None:
                client = httpx.Client(transport=self._transport, headers=kopf, timeout=self._timeout)
            else:
                client = httpx.Client(
                    verify=self._ssl_kontext(identity), headers=kopf, timeout=self._timeout
                )
        except PenEchoNichtErreichbar:
            raise
        except Exception as fehler:
            log.warning("PenEcho-Verbindung nicht aufgebaut: %s", _typ(fehler))
            raise PenEchoNichtErreichbar("verbindung") from None
        self._client = client
        try:
            self._rpc("initialize", {
                "protocolVersion": PROTOKOLL,
                "capabilities": {},
                "clientInfo": {"name": "learning-huelle", "version": "1"},
            })
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except PenEchoNichtErreichbar:
            self._zuruecksetzen()
            raise
        except Exception as fehler:
            log.warning("PenEcho-Handshake fehlgeschlagen: %s", _typ(fehler))
            self._zuruecksetzen()
            raise PenEchoNichtErreichbar(_typ(fehler)) from None

    def _zuruecksetzen(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._mcp_session = None

    def _post(self, body: dict[str, Any], timeout: float | None = None) -> httpx.Response:
        assert self._client is not None
        kopf = {"Content-Type": "application/json"}
        if self._mcp_session:
            kopf["Mcp-Session-Id"] = self._mcp_session
            kopf["MCP-Protocol-Version"] = PROTOKOLL
        if timeout is None:
            antwort = self._client.post(self._url, json=body, headers=kopf)
        else:
            antwort = self._client.post(self._url, json=body, headers=kopf, timeout=timeout)
        antwort.raise_for_status()
        return antwort

    def _rpc(self, methode: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self._next_id += 1
        try:
            antwort = self._post(
                {"jsonrpc": "2.0", "id": self._next_id, "method": methode, "params": params}, timeout)
            sid = antwort.headers.get("mcp-session-id")
            if sid and not self._mcp_session:
                self._mcp_session = sid
            daten = antwort.json()
        except Exception as fehler:
            log.warning("PenEcho-Aufruf %s fehlgeschlagen: %s", methode, _typ(fehler))
            raise _HttpFehler(_typ(fehler), getattr(getattr(fehler, "response", None), "status_code", None)) from None
        if not isinstance(daten, dict) or "error" in daten or not isinstance(daten.get("result"), dict):
            log.warning("PenEcho-Aufruf %s abgelehnt", methode)
            raise PenEchoNichtErreichbar("jsonrpc")
        return daten["result"]

    def _werkzeug(self, name: str, argumente: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """`timeout` (Sekunden) begrenzt den einen HTTP-Aufruf; dann gibt es auch keine Wiederholung."""
        for versuch in (1, 2):
            self._verbinden()
            try:
                ergebnis = self._rpc("tools/call", {"name": name, "arguments": argumente}, timeout)
            except _HttpFehler as fehler:
                # Der MCP-Zugang verfaellt nach Leerlauf (404/400): neu verbinden, einmal wiederholen.
                self._zuruecksetzen()
                if versuch == 1 and fehler.status in (400, 404) and timeout is None:
                    continue
                raise
            except PenEchoNichtErreichbar:
                self._zuruecksetzen()
                raise
            break
        if ergebnis.get("isError"):
            struktur = self._struktur(ergebnis)
            code = struktur.get("code")
            # Nur PenEchos feste Codes (z. B. LAYOUT_CONFLICT) ins Log, nie Meldungstext.
            sicher = code if isinstance(code, str) and _CODE_MUSTER.fullmatch(code) else "?"
            log.warning("PenEcho-Werkzeug %s meldet Fehler %s", name, sicher)
            if isinstance(code, str) and code in _SITZUNG_CODES:
                raise PenEchoSitzungUnbekannt(code)
            if isinstance(code, str) and code in _VERLOREN_CODES:
                raise PenEchoFlaecheVerloren(code)
            if code == "LAYOUT_CONFLICT":
                raise PenEchoPlatzBelegt(code, _hindernisse(struktur))
            if code == "REVISION_CONFLICT":
                raise PenEchoRevisionVeraltet(code)
            if code == "OBJECT_LIMIT":
                raise PenEchoFlaecheVoll(code)
            if code == "request_limit":
                self._sitzung_schliessen(argumente.get("sessionId"))
                raise PenEchoAnfragenStau(code)
            if code == "REQUEST_ID_CONFLICT":
                raise PenEchoAnfrageSchonBekannt(code)
            if code == "ACTIVE_CANVAS_REQUIRED":
                raise PenEchoNichtSichtbar(code)
            if code in ("SOURCE_CONFLICT", "PATCH_CONFLICT"):
                raise PenEchoQuelleVeraltet(code)
            if code == "session_key_conflict":
                raise PenEchoSchluesselBelegt(code)
            raise PenEchoNichtErreichbar("werkzeug")
        return ergebnis

    def _belegung(self, session_id: str, region: dict[str, int]) -> list[dict[str, Any]]:
        """Objekte in `region` (penecho_list_files, nur lesend), je {objectId, kind, bounds}.

        Abnahme 06.11. (F18): "steht schon da" per LAYOUT_CONFLICT zu erkennen hinterliess je Probe einen
        unaufgeloesten Eintrag in PenEchos Sitzung (siehe PenEchoAnfragenStau). Lesen zaehlt dort nicht.
        Nicht lesbar: leere Liste, dann gilt wie bisher die Probe beim Setzen. Hoechstens 5 Seiten.
        """
        belegung: list[dict[str, Any]] = []
        versatz: object = 0
        try:
            for _ in range(5):
                seite = self._struktur(self._werkzeug("penecho_list_files", {
                    "sessionId": session_id, "region": region, "offset": versatz}))
                for e in seite.get("entries") or []:
                    b = e.get("bounds") if isinstance(e, dict) else None
                    if (isinstance(e.get("objectId"), str) and isinstance(e.get("kind"), str) and isinstance(b, dict)
                            and all(isinstance(b.get(k), (int, float)) and not isinstance(b.get(k), bool) for k in "xywh")):
                        belegung.append({"objectId": e["objectId"], "kind": e["kind"], "bounds": b})
                versatz = seite.get("nextOffset")
                if not isinstance(versatz, int) or isinstance(versatz, bool):
                    break
        except (PenEchoSitzungUnbekannt, PenEchoFlaecheVerloren):
            raise
        except PenEchoNichtErreichbar as fehler:
            log.warning("Belegung nicht gelesen: %s", _typ(fehler))
        return belegung

    def _sitzung_schliessen(self, session_id: object) -> None:
        """penecho_close_session: die Sitzung endet, der Inhalt des Boards bleibt (schema.js)."""
        if not isinstance(session_id, str) or not session_id:
            return
        try:
            self._werkzeug("penecho_close_session", {"sessionId": session_id})
        except PenEchoNichtErreichbar as fehler:
            log.warning("PenEcho-Sitzung nicht geschlossen: %s", _typ(fehler))

    @staticmethod
    def _struktur(ergebnis: dict[str, Any]) -> dict[str, Any]:
        sc = ergebnis.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        for teil in ergebnis.get("content") or []:
            if isinstance(teil, dict) and teil.get("type") == "text":
                try:
                    wert = json.loads(teil.get("text") or "")
                except ValueError:
                    continue
                if isinstance(wert, dict):
                    return wert
        return {}

    # ---- Merkdatei --------------------------------------------------------
    def _gemerkt(self, datei: Path | None = None) -> dict[str, str]:
        try:
            wert = json.loads((datei or self._merkdatei).read_text(encoding="utf-8"))
            return {k: v for k, v in wert.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _merken(self, karte: dict[str, str], datei: Path | None = None) -> None:
        ziel = datei or self._merkdatei
        ziel.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=ziel.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(karte, f, ensure_ascii=False)
            os.replace(tmp, ziel)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def vergessen(self, schluessel: str) -> None:
        """Entfernt die gemerkte sessionId zu einem Schluessel (naechstes sitzung() startet neu)."""
        karte = self._gemerkt()
        if schluessel in karte:
            del karte[schluessel]
            self._merken(karte)

    def dokumente(self, schluessel: Iterable[str]) -> frozenset[str]:
        """Die gemerkten documentIds der Boards dieser Lernsitzungen (`dokumente.json`); nur lesend.

        Fuer die Ablage-Pruefung des Durchreichers: eine Person erreicht in PenEchos Server-Ablage nur Boards
        mit diesen documentIds.
        """
        karte = self._gemerkt(self._dokumentdatei)
        return frozenset(karte[k] for k in schluessel if karte.get(k))

    # ---- Oeffentliche Methoden -------------------------------------------
    def sitzung(self, schluessel: str, titel: str, canvas_id: str | None = None) -> str:
        """PenEcho-Sitzung (eine Flaeche) zu `schluessel`, der Learning-Sitzungs-ID (Ruling L).

        schema.js:656-658 penecho_start_session: title (Pflicht), sessionKey, restore (bool).
        Die Merkdatei haelt schluessel -> sessionId; ein bekannter Schluessel startet keine neue Sitzung.
        Der Titel zaehlt nur beim ersten Anlegen.
        Nach einem Neustart findet PenEcho das Board ueber den sessionKey wieder. Meldet PenEcho die
        Flaeche als verloren, wird das gemerkte Dokument (dokumente.json) in jedem verbundenen Browser
        gesucht; erst wenn es nirgends liegt, entsteht eine neue Flaeche unter neuem Schluessel.

        `canvas_id` (Teil B): die Flaechen-Kennung des Tabs der Person (`lf-<32 hex>`, von der Bruecke geprueft).
        Dann bindet die Sitzung genau an diesen Tab (`_sitzung_im_tab`). Ohne sie bleibt alles wie oben;
        eine schon an einen Tab gebundene Sitzung bleibt dabei, wo sie ist.
        """
        if canvas_id is not None:
            return self._sitzung_im_tab(schluessel, titel, canvas_id)
        bekannt = self._gemerkt().get(schluessel)
        if bekannt:
            return bekannt
        ersatz = self._gemerkt(self._ersatzdatei)
        dokumente = self._gemerkt(self._dokumentdatei)
        argumente = {
            "title": titel,
            "sessionKey": ersatz.get(schluessel) or _sitzungsschluessel(schluessel),
            "restore": True,
        }
        try:
            ergebnis = self._werkzeug("penecho_start_session", argumente)
        except PenEchoFlaecheVerloren:
            ergebnis = self._gemerktes_board_oeffnen(argumente, dokumente.get(schluessel))
            if ergebnis is None:
                # Das Board liegt in keinem verbundenen Browser und nicht in PenEchos Ablage:
                # neue Flaeche, neuer Schluessel.
                log.warning("PenEcho-Board nicht gefunden, neue Flaeche")
                neu = _sitzungsschluessel(schluessel)[: MAX_SCHLUESSEL - 9] + "#" + uuid4().hex[:8]
                ergebnis = self._werkzeug("penecho_start_session", {**argumente, "sessionKey": neu})
                ersatz[schluessel] = neu
                self._merken(ersatz, self._ersatzdatei)
        struktur = self._struktur(ergebnis)
        session_id = struktur.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise PenEchoNichtErreichbar("sessionId")
        self._dokument_merken(schluessel, struktur.get("documentId"), dokumente)
        karte = self._gemerkt()
        karte[schluessel] = session_id
        self._merken(karte)
        return session_id

    def _sitzung_im_tab(self, schluessel: str, titel: str, canvas_id: str) -> str:
        """Sitzung zu `schluessel` auf dem Tab `canvas_id` (Teil B: eine Flaeche je Person und Tab).

        Gemerkt und an diesen Tab gebunden: dieselbe Sitzung. Sonst penecho_start_session mit canvasId und, wenn
        gemerkt, der documentId des Boards; PenEcho oeffnet dann genau dieses Board in diesem Tab
        (canvas-documents.js, mcp_start_session mit documentId). So uebernimmt ein neuer Tab derselben Person ihr
        Board. Der sessionKey ist je Lernsitzung und Tab eigen, denn PenEcho bindet einen Schluessel fest an eine
        canvasId (service.js, session_key_conflict). Nie die Suche in allen Browsern wie bei `sitzung` ohne Tab:
        die anderen Tabs sind die Flaechen anderer Personen. Gemerkt wird nur ein Hash der Kennung.
        """
        marke = _tab_marke(canvas_id)
        bekannt = self._gemerkt().get(schluessel)
        tabs = self._gemerkt(self._tabdatei)
        if bekannt and tabs.get(schluessel) == marke:
            return bekannt
        dokumente = self._gemerkt(self._dokumentdatei)
        # Wie `ersatz` ohne Tab: ein frischer Schluessel bleibt gemerkt (flaechen.json, unter `<sid>@<marke>`, nie
        # unter dem Schluessel ohne Tab), sonst kaeme nach `vergessen` oder einem Neustart erst wieder der Konflikt.
        tab_schluessel = f"{schluessel}@{marke}"
        ersatz = self._gemerkt(self._ersatzdatei)
        basis = _sitzungsschluessel(tab_schluessel)
        frisch = basis[: MAX_SCHLUESSEL - 9] + "#" + uuid4().hex[:8]
        argumente: dict[str, Any] = {
            "title": titel, "sessionKey": ersatz.get(tab_schluessel) or basis, "restore": True,
            "canvasId": canvas_id,
        }
        if dokumente.get(schluessel):
            argumente["documentId"] = dokumente[schluessel]
        try:
            try:
                ergebnis = self._werkzeug("penecho_start_session", argumente)
            except PenEchoSchluesselBelegt:
                # Der Schluessel dieses Tabs zeigt bei PenEcho noch auf ein frueheres Board der Lernsitzung (das Board
                # wurde inzwischen ersetzt): derselbe Tab, das aktuelle Board, ein frischer Schluessel.
                ergebnis = self._werkzeug("penecho_start_session", {**argumente, "sessionKey": frisch})
                ersatz[tab_schluessel] = frisch
        except PenEchoFlaecheVerloren:
            # Das Board liegt weder in diesem Tab noch in PenEchos Ablage (auch nach dem Wiederholungsversuch mit
            # frischem Schluessel): ein neues Board in diesem Tab, frischer Schluessel.
            log.warning("PenEcho-Board nicht gefunden, neue Flaeche im Tab")
            ohne = {k: v for k, v in argumente.items() if k != "documentId"}
            ergebnis = self._werkzeug("penecho_start_session", {**ohne, "sessionKey": frisch})
            ersatz[tab_schluessel] = frisch
        if ersatz.get(tab_schluessel) == frisch:
            self._merken(ersatz, self._ersatzdatei)
        struktur = self._struktur(ergebnis)
        session_id = struktur.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise PenEchoNichtErreichbar("sessionId")
        self._dokument_merken(schluessel, struktur.get("documentId"), dokumente)
        karte = self._gemerkt()
        karte[schluessel] = session_id
        self._merken(karte)
        tabs[schluessel] = marke
        self._merken(tabs, self._tabdatei)
        return session_id

    def _gemerktes_board_oeffnen(self, argumente: dict[str, Any], dokument: str | None) -> dict[str, Any] | None:
        """Oeffnet das gemerkte Dokument im ersten verbundenen Browser, der es hat, und bindet die Sitzung.

        schema.js penecho_list_canvases (ohne Argumente) und penecho_open_canvas
        (instanceId, canvasId, requestId Pflicht; documentId). Ohne Treffer None, ebenso wenn
        die Bindung an das gefundene Dokument scheitert; sitzung() legt dann eine Ersatzflaeche an.
        Ein Fehler in einem Browser bricht die Suche nicht ab, es folgt der naechste.
        """
        if not dokument:
            return None
        liste = self._struktur(self._werkzeug("penecho_list_canvases", {}))
        browser: list[tuple[str, str]] = []
        for eintrag in liste.get("canvases") or []:
            if not isinstance(eintrag, dict):
                continue
            ziel = (eintrag.get("instanceId") or liste.get("instanceId"), eintrag.get("canvasId"))
            if all(isinstance(t, str) and t for t in ziel) and ziel not in browser:
                browser.append(ziel)
        for instanz, canvas in browser:
            try:
                self._werkzeug("penecho_open_canvas", {
                    "instanceId": instanz,
                    "canvasId": canvas,
                    "documentId": dokument,
                    "requestId": "oeffnen-" + uuid4().hex,
                })
            except PenEchoNichtErreichbar:  # auch PenEchoFlaecheVerloren
                continue
            try:
                return self._werkzeug("penecho_start_session", {**argumente, "documentId": dokument})
            except PenEchoNichtErreichbar:
                return None
        return None

    def _dokument_merken(self, schluessel: str, dokument: Any, dokumente: dict[str, str] | None = None) -> None:
        if not isinstance(dokument, str) or not dokument:
            return
        karte = self._gemerkt(self._dokumentdatei) if dokumente is None else dokumente
        if karte.get(schluessel) != dokument:
            karte[schluessel] = dokument
            self._merken(karte, self._dokumentdatei)

    def dokument_merken(self, session_id: str) -> bool:
        """Merkt die documentId des Boards dieser Sitzung in `dokumente.json`. Speichert NICHTS.

        PenEcho 1.4.2 hat kein MCP-Werkzeug zum Speichern (BOARD_SPEICHERN.md). Gespeichert wird
        nur ueber PenEchos eigenen Knopf; den loest die Seite ueber `flaecheSpeichern()` aus
        (`seite/flaeche.js`, Skript `flaeche/speichern.js` im Durchreicher). Diese Methode fragt
        PenEcho nur nach dem Dokument der Sitzung (`penecho_inspect_session`) und merkt dessen ID,
        damit sitzung() nach einem Verlust genau dieses Board in den Browsern sucht. Wiederholbar.

        True: gemerkt. False: PenEcho kennt die Sitzung, die Huelle hat aber keine Lernsitzung mit
        dieser session_id (nichts gemerkt). Fehler von PenEcho und eine Antwort ohne documentId
        kommen als PenEchoNichtErreichbar.
        """
        ergebnis = self._werkzeug("penecho_inspect_session", {"sessionId": session_id})
        dokument = self._struktur(ergebnis).get("documentId")
        if not isinstance(dokument, str) or not dokument:
            raise PenEchoNichtErreichbar("documentId")
        schluessel = [k for k, sid in self._gemerkt().items() if sid == session_id]
        if not schluessel:
            log.info("dokument_merken: keine Lernsitzung zu dieser Sitzung, nichts gemerkt")
            return False
        dokumente = self._gemerkt(self._dokumentdatei)
        for k in schluessel:
            self._dokument_merken(k, dokument, dokumente)
        return True

    def aufgabe_setzen(
        self, session_id: str, aufgabe_id: str, text: str, nummer: int, *, mit_ergebnisfeld: bool = False,
        mit_arbeitsfeld: bool = False, bilder: Sequence[Aufgabenbild] = (), rechteck: dict[str, int] | None = None,
        zeigen: bool = True,
    ) -> None:
        """Aufgabe `nummer` in ihren Bereich: `bereich(nummer)` oder, im Listenkurs, `rechteck` (ohne Grenze 1..20).

        Ohne Rechteck holt PenEcho die Aufgabe danach ins Bild. Mit Rechteck wird das Board nur sichtbar
        gemacht (der Rahmen braucht es aktiv); den Bereich zeigt die Huelle selbst (`bereich_zeigen`).
        `zeigen=False` (Setzen im Hintergrund): kein show, denn jedes show macht dieses Board in der
        Lernflaeche zum aktiven. Ist das Board dann nicht aktiv, entfaellt nur der Rahmen.
        """
        # schema.js:665 penecho_edit_canvas: action create_text braucht text, optional region (x/y).
        # Feste requestId: dieselbe Aufgabe am selben Platz wiederholt PenEcho nicht (Idempotenz).
        b = rechteck if rechteck is not None else bereich(nummer)
        f = felder(b)
        region = f.textfeld
        hinweis = _HINWEIS_ERGEBNIS if mit_ergebnisfeld else _HINWEIS_LOESUNG if mit_arbeitsfeld else None
        argumente = {
            "sessionId": session_id,
            "requestId": f"aufgabe-{aufgabe_id}-{nummer}"[:MAX_SCHLUESSEL],
            "action": "create_text",
            "text": f"Aufgabe {nummer}: {text}" + (f"\n\n{hinweis}" if hinweis else ""),
            "region": region,
        }
        voll = False
        belegung = self._belegung(session_id, b)
        try:
            objekt_id = _an_ecke(belegung, region, "text")
            if objekt_id is None:
                objekt_id = self._struktur(self._werkzeug("penecho_edit_canvas", argumente)).get("objectId")
        except PenEchoFlaecheVoll:
            voll = True
            # PenEcho prueft die Grenze vor dem Platz: auch eine schon vorhandene Aufgabe scheitert hier.
            # Board und Rahmen trotzdem, damit die Flaeche verbunden bleibt; der Text steht in der Seitenleiste.
            log.warning("Aufgabentext nicht gesetzt: Flaeche voll")
            objekt_id = None
        except PenEchoPlatzBelegt as fehler:
            eigene = _eigene_texte(fehler.hindernisse, region)
            if eigene:
                # Die Aufgabe steht schon da (Board nach Neustart wiederhergestellt, neue PenEcho-Sitzung
                # kennt die feste requestId nicht): nicht doppelt setzen.
                objekt_id = eigene[0]["id"]
            else:
                # Jemand hat den Textplatz belegt (z. B. PenEchos KI nach einer Abgabe): PenEcho waehlt frei.
                del argumente["region"]
                argumente["requestId"] = f"aufgabe-{aufgabe_id}-{nummer}-frei"[:MAX_SCHLUESSEL]
                objekt_id = self._struktur(self._werkzeug("penecho_edit_canvas", argumente)).get("objectId")
        # Vor dem Rahmen: draw_ink verlangt ein sichtbares Board (ACTIVE_CANVAS_REQUIRED).
        if zeigen:
            self._zeigen(session_id, objekt_id if rechteck is None else None)
        if bilder:
            # Vor dem Rahmen: dessen Tinte zaehlte sonst als Hindernis im Bildfeld.
            # Volle Flaeche: keine Beschriftungen versuchen. Jeder gescheiterte Text bliebe in PenEchos
            # Sitzung unaufgeloest und sperrte nach vier weiteren Fehlern alle Aenderungen (F17).
            self._bilder_setzen(session_id, aufgabe_id, nummer, bilder,
                                bildfeld(nummer, unter_ergebnisfeld=mit_ergebnisfeld, rechteck=b),
                                beschriften=not voll, belegung=belegung)
        if mit_ergebnisfeld:
            self._feld_zeichnen(session_id, aufgabe_id, nummer, f.ergebnisfeld, "ergebnis", zeigen=zeigen)
        elif mit_arbeitsfeld:
            self._feld_zeichnen(session_id, aufgabe_id, nummer, f.arbeitsfeld, "arbeit", zeigen=zeigen)

    def _bilder_setzen(
        self, session_id: str, aufgabe_id: str, nummer: int, bilder: Sequence[Aufgabenbild], feld: dict[str, int],
        *, beschriften: bool = True, belegung: Sequence[dict[str, Any]] = (),
    ) -> None:
        """Aufgabenbilder mit Beschriftung im Raster ins Bildfeld (penecho_place_image mit Region).

        Ist ein Platz belegt, steht das Bild meist schon da (Board nach Neustart wiederhergestellt): dann
        nichts setzen, auch nicht anderswo. Ein fehlendes Bild bricht die Aufgabe nicht ab. Ist die Flaeche
        voll (OBJECT_LIMIT), folgen nur noch Bilder, keine Beschriftungen mehr. Was laut `belegung`
        (siehe _belegung) schon an seinem Platz liegt, wird nicht erneut gesendet; ein fehlendes Schild
        zu einem vorhandenen Bild wird ergaenzt.
        """
        for position, (bild, (schild, platz)) in enumerate(zip(bilder, bildraster(feld, bilder)), start=1):
            quelle = _datenurl(bild.daten)
            if quelle is None or len(quelle) > MAX_BILD_DATENURL or min(platz["w"], platz["h"]) < _BILD_MIN:
                log.warning("Aufgabenbild %d uebersprungen: Format oder Groesse", position)
                continue
            try:
                if _an_ecke(belegung, platz, "image") is None:
                    self._werkzeug("penecho_place_image", {
                        "sessionId": session_id,
                        "requestId": self._feld_id("bild", aufgabe_id, nummer, f"-{position}"),
                        "source": quelle,
                        "width": platz["w"],
                        "region": platz,
                    })
                if (beschriften and schild is not None and bild.beschriftung
                        and _an_ecke(belegung, schild, "text") is None):
                    self._werkzeug("penecho_edit_canvas", {
                        "sessionId": session_id,
                        "requestId": self._feld_id("bild", aufgabe_id, nummer, f"-{position}-schild"),
                        "action": "create_text",
                        "text": bild.beschriftung[:200],
                        "region": schild,
                    })
            except (PenEchoSitzungUnbekannt, PenEchoFlaecheVerloren):
                raise
            except PenEchoPlatzBelegt:
                log.info("Aufgabenbild %d steht schon da", position)
            except PenEchoFlaecheVoll:
                beschriften = False
                log.warning("Aufgabenbild %d: Flaeche voll, keine Beschriftung mehr", position)
            except PenEchoNichtErreichbar as fehler:
                log.warning("Aufgabenbild %d nicht gesetzt: %s", position, _typ(fehler))

    def _zeigen(self, session_id: str, objekt_id: object = None, region: dict[str, int] | None = None) -> None:
        """Board sichtbar machen und die Aufgabe ins Bild holen (show). Ab Aufgabe 2 laege sie sonst
        unter dem Bildschirmrand; ein frisch angelegtes Board ist ohne show nicht aktiv.

        Zierde wie der Rahmen: nur verlorene Sitzung/Flaeche gehen an den Aufrufer. Eine neue requestId je
        Aufruf, sonst spielte PenEcho beim erneuten Oeffnen nur die alte Antwort ab und zeigte nichts.
        """
        argumente: dict[str, Any] = {"sessionId": session_id, "requestId": f"zeigen-{uuid4().hex}", "action": "show"}
        if region is not None:
            argumente["region"] = dict(region)
        elif isinstance(objekt_id, str) and objekt_id:
            argumente["objectId"] = objekt_id[:MAX_SCHLUESSEL]
        try:
            self._werkzeug("penecho_edit_canvas", argumente)
        except (PenEchoSitzungUnbekannt, PenEchoFlaecheVerloren):
            raise
        except PenEchoNichtErreichbar as fehler:
            log.warning("Aufgabe nicht gezeigt: %s", _typ(fehler))

    @staticmethod
    def _feld_id(praefix: str, aufgabe_id: str, nummer: int, zusatz: str = "") -> str:
        # Der Zusatz bleibt immer erhalten, nur die aufgabe_id wird bei Bedarf gekuerzt.
        rest = MAX_SCHLUESSEL - len(f"{praefix}--{nummer}{zusatz}")
        return f"{praefix}-{aufgabe_id[:rest]}-{nummer}{zusatz}"

    def _feld_zeichnen(
        self, session_id: str, aufgabe_id: str, nummer: int, feld: dict[str, int], praefix: str, *, zeigen: bool = True,
    ) -> None:
        """Tintenrahmen um das Feld (der Hinweis darauf steht im Aufgabentext, siehe _HINWEIS_ERGEBNIS).

        Der Rahmen ist Tinte (penecho_edit_canvas draw_ink), kein Objekt: Tinte ist nur Pixel in der
        Zeichenebene, faengt also keine Stifteingabe ab, und die Lernenden schreiben ungehindert
        darueber. penecho_draw/Widgets sind eigene Objekte und wuerden Eingaben aufnehmen; zudem
        platziert der Host sie selbst, nicht an Weltkoordinaten. Der Rahmen ist Zierde: Fehler ausser
        verlorener Sitzung/Flaeche brechen die Aufgabe nicht ab (der Schnappschuss gilt ohnehin dem
        festen Feld).
        """
        a = _RAHMEN_ABSTAND
        links, oben = feld["x"] - a, feld["y"] - a
        rechts, unten = feld["x"] + feld["w"] + a, feld["y"] + feld["h"] + a
        rahmen = [{"x": links, "y": oben}, {"x": rechts, "y": oben}, {"x": rechts, "y": unten},
                  {"x": links, "y": unten}, {"x": links, "y": oben}]
        for versuch in (1, 2):
            try:
                # PenEchos Signatur einer Anfrage umfasst baseRevision: eine neue Revision unter
                # derselben requestId waere REQUEST_ID_CONFLICT. Der erste Versuch behaelt die feste
                # Kennung (erneutes Senden nach verlorener Antwort wird dedupliziert), der Wiederholungs-
                # versuch nach REVISION_CONFLICT bekommt eine abgeleitete frische Kennung.
                self._werkzeug("penecho_edit_canvas", {
                    "sessionId": session_id,
                    "requestId": self._feld_id(praefix, aufgabe_id, nummer, "" if versuch == 1 else "-r2"),
                    "action": "draw_ink",
                    "baseRevision": self._revision(session_id),
                    "strokes": [{"points": rahmen, "color": _RAHMEN_FARBE, "width": _RAHMEN_STAERKE}],
                })
                return
            except PenEchoAnfrageSchonBekannt:
                if versuch == 1:
                    # Nur beim Erstversuch heisst REQUEST_ID_CONFLICT "schon gezeichnet": dieselbe feste
                    # Kennung kam in dieser Sitzung schon an (Wiederholung der Aufgabe, andere Revision).
                    return
                log.warning("Feld-Rahmen nicht gezeichnet: REQUEST_ID_CONFLICT")
                return
            except PenEchoRevisionVeraltet:
                if versuch == 1:
                    continue  # Flaeche hat sich geaendert: Revision neu lesen, einmal wiederholen
                log.warning("Feld-Rahmen nicht gezeichnet: Revision veraltet")
            except PenEchoNichtSichtbar:
                if versuch == 1 and zeigen:
                    self._zeigen(session_id)  # Board inzwischen verdeckt: zeigen, einmal wiederholen
                    continue
                if not zeigen:
                    log.warning("Feld-Rahmen nicht gezeichnet: Board nicht aktiv, im Hintergrund kein show")
                    return
                log.warning("Feld-Rahmen nicht gezeichnet: Board nicht sichtbar")
            except (PenEchoSitzungUnbekannt, PenEchoFlaecheVerloren):
                raise
            except PenEchoNichtErreichbar as fehler:
                log.warning("Feld-Rahmen nicht gezeichnet: %s", _typ(fehler))
                return

    def _revision(self, session_id: str) -> int:
        """Aktuelle Revision der Flaeche (canvas.json), Pflicht fuer draw_ink als baseRevision."""
        ergebnis = self._werkzeug("penecho_read_file", {"sessionId": session_id, "path": "canvas.json"})
        inhalt = self._struktur(ergebnis).get("content")
        try:
            revision = json.loads(inhalt)["revision"] if isinstance(inhalt, str) else None
        except (ValueError, KeyError, TypeError):
            revision = None
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise PenEchoNichtErreichbar("revision")
        return revision

    def rueckmeldung_setzen(
        self, session_id: str, aufgabe_id: str, nummer: int, titel: str, text: str, *,
        rechteck: dict[str, int] | None = None,
    ) -> None:
        """Rueckmeldung als Text in `rueckmeldungsfeld(nummer)`; eine neue ersetzt die alte.

        Kein Widget mehr: PenEcho platzierte die Karten selbst und belegte damit die Bereiche der
        naechsten Aufgaben. Liegt am Platz schon eine fruehere Rueckmeldung (ein Text exakt an der
        Feldecke), wird sie geloescht und einmal neu gesetzt. Jedes andere Hindernis (Handschrift,
        Widget, Text der Lernenden) bleibt; dann PenEchoPlatzBelegt, die Bruecke zeigt die Leiste.
        Mit `rechteck` liegt das Feld neben diesem Bereich (Listenkurs).
        """
        # Ohne Rechteck ausserhalb 1..20 ValueError, ohne PenEcho zu rufen.
        feld = felder(rechteck).rueckmeldungsfeld if rechteck is not None else rueckmeldungsfeld(nummer)
        # Zu lang fuer das Feld: gekuerzt; die volle Rueckmeldung bleibt in Learning (Bericht). Die Zeilen
        # richten sich nach der Hoehe des Feldes: hohe Bereiche (Listenkurs, Aufgabe mit Bild) fassen mehr (A2).
        zeilen = max(_RUECKMELDUNG_ZEILEN, feld["h"] // _ZEILEN_HOEHE)
        inhalt = kuerzen(f"{titel}\n\n{text}", zeichen=_TEXT_HOECHSTENS, zeilen=zeilen,
                         je_zeile=_RUECKMELDUNG_ZEICHEN_JE_ZEILE)
        pruefsumme = hashlib.sha256(inhalt.encode("utf-8")).hexdigest()[:12]
        rest = MAX_SCHLUESSEL - len(f"rueckmeldung--{nummer}-{pruefsumme}")
        argumente = {
            "sessionId": session_id,
            # Gleicher Inhalt, gleiche Kennung: PenEcho haelt einen Fehlversuch offen, der zweite
            # Versuch nach dem Loeschen schliesst ihn ab (bound-operations.js mutationRequests).
            "requestId": f"rueckmeldung-{aufgabe_id[:rest]}-{nummer}-{pruefsumme}",
            "action": "create_text",
            "text": inhalt,
            "region": feld,
        }
        try:
            self._werkzeug("penecho_edit_canvas", argumente)
            return
        except PenEchoPlatzBelegt as fehler:
            alte = _eigene_texte(fehler.hindernisse, feld)
            if not alte or len(alte) != len(fehler.hindernisse):
                raise
        for h in alte:
            self._werkzeug("penecho_edit_canvas", {
                "sessionId": session_id,
                "requestId": f"weg-{uuid4().hex}",
                "action": "delete",
                "objectId": h["id"][:MAX_SCHLUESSEL],
                "baseRevision": self._revision(session_id),
            })
        self._werkzeug("penecho_edit_canvas", argumente)

    def _karte(self, session_id: str, artifact_id: str, titel: str, text: str) -> None:
        # schema.js:675-677 penecho_present_widget: sessionId, artifactId, title + genau eines
        # aus html/architecture/sequence/workflow/scene/note
        # schema.js:703: requestId ist Pflicht und "nur fuer identische Argumente wiederverwenden";
        # daher traegt sie einen Kurz-Hash der Argumente (deterministisch, <= 128 Zeichen).
        signatur = f"{titel}\n{text}"
        pruefsumme = hashlib.sha256(signatur.encode("utf-8")).hexdigest()[:12]
        rest = MAX_SCHLUESSEL - len(pruefsumme) - 1
        argumente: dict[str, Any] = {
            "sessionId": session_id,
            "requestId": f"{artifact_id[:rest]}-{pruefsumme}",
            "artifactId": artifact_id[:MAX_SCHLUESSEL],
            "title": titel,
            "note": {
                "title": titel,
                "style": "note",
                "summary": text,
                "blocks": [{"type": "markdown", "text": text}],
            },
        }
        self._werkzeug("penecho_present_widget", argumente)

    def bericht_zeigen(
        self, session_id: str, sid: str, text: str, nummern: Sequence[int] = (), *,
        rechteck: Mapping[int, dict[str, int]] | None = None,
    ) -> None:
        """Berichtskarte `bericht-{sid}` und, je Aufgabe, der Schnappschuss ihres Bereichs.

        Wiederholbar: die Karte hat eine feste artifactId, jedes Bild eine feste requestId
        `bericht-{sid}-{nummer}`. Ein Bild, das nicht passt (zu gross, schon gesetzt, PenEcho lehnt ab),
        faellt weg; die Karte bleibt. Jedes Bild steht an `berichtsplatz(nummer)` neben seiner Aufgabe;
        liegt dort laut Belegung schon etwas (das Bild selbst oder ein fremdes Objekt), wird es weder
        aufgenommen noch gesendet. Der erste Transportfehler beendet die Bildschleife,
        ebenso ein Zeitbudget von `BERICHT_BILD_ZEIT` fuer alle Bilder; die Karte steht dann schon.
        Nur verlorene Sitzung/Flaeche gehen an den Aufrufer.

        `rechteck` (Listenkurs): Bereich je Nummer; Bild und Platz kommen dann aus diesem Rechteck.
        Eine Nummer ohne Eintrag nimmt `bereich(nummer)`.
        """
        erste = text.splitlines()[0] if text.strip() else ""
        titel = re.sub(r"\\(.)", r"\1", erste.lstrip("# ").strip()) or "Bericht"
        if len(titel) > MAX_TITEL:
            titel = titel[: MAX_TITEL - 1].rstrip() + "\u2026"
        self._karte(session_id, f"bericht-{sid}", titel, text)
        if not BERICHT_MIT_SCHNAPPSCHUESSEN:
            return
        ende = _uhr() + BERICHT_BILD_ZEIT
        bereiche = dict(rechteck or {})

        def platz_von(nummer: int) -> dict[str, int]:
            r = bereiche.get(nummer)
            return felder(r).berichtsplatz if r is not None else berichtsplatz(nummer)

        if bereiche:
            plaetze = []
            for n in nummern:
                try:
                    plaetze.append(platz_von(n))
                except ValueError:
                    continue  # die Schleife unten ueberspringt diese Nummer ebenso
            belegung = []
            for spalte in _spalten(plaetze):
                belegung += self._belegung(session_id, spalte)
        else:
            spalte = berichtsplatz(1)
            belegung = self._belegung(session_id, {**spalte, "h": BEREICHE * _ZEILEN_SCHRITT})
        for position, nummer in enumerate(nummern):
            if _uhr() >= ende:
                log.warning("Berichtsbilder: Zeitbudget erschoepft, %d uebersprungen", len(nummern) - position)
                break
            try:
                platz = platz_von(nummer)
                if any(_ueberlappt(e["bounds"], platz) for e in belegung):
                    log.info("Berichtsbild %d: Platz schon belegt", nummer)
                    continue
                quelle = _datenurl(self.schnappschuss(session_id, nummer, rechteck=bereiche.get(nummer)))
                if quelle is None or len(quelle) > MAX_BILD_DATENURL:
                    log.warning("Berichtsbild uebersprungen: Format oder Groesse")
                    continue
                self._werkzeug("penecho_place_image", {
                    "sessionId": session_id,
                    "requestId": f"bericht-{sid}-{nummer}"[:MAX_SCHLUESSEL],
                    "source": quelle,
                    "width": BERICHT_BILD_BREITE,
                    "region": platz,
                })
            except (PenEchoSitzungUnbekannt, PenEchoFlaecheVerloren):
                raise
            except (PenEchoAnfrageSchonBekannt, PenEchoPlatzBelegt, PenEchoRevisionVeraltet, ValueError) as fehler:
                # Dieses eine Bild entfaellt (steht schon, Platz belegt, Revision veraltet, Bereich ungueltig).
                log.warning("Berichtsbild uebersprungen: %s", _typ(fehler))
            except PenEchoNichtErreichbar as fehler:
                # Transportfehler: weitere Bilder liefen in dieselbe Zeitueberschreitung.
                log.warning("Berichtsbilder abgebrochen: %s", _typ(fehler))
                break

    def schild_setzen(self, session_id: str, request_id: str, text: str, rechteck: dict[str, int]) -> None:
        """Namensschild einer Mappe (Teil C) als Text in `rechteck` (Kopf der Mappe).

        Feste `request_id` (`mappe-<gruppe>-<person>`): ein erneutes Setzen derselben Mappe wiederholt PenEcho nicht.
        Steht dort schon ein Text (Board wiederhergestellt), wird nichts gesendet; belegt PenEcho den Platz
        (LAYOUT_CONFLICT), steht das Schild schon da. Andere Fehler gehen an den Aufrufer.
        """
        if _an_ecke(self._belegung(session_id, rechteck), rechteck, "text") is not None:
            return
        try:
            self._werkzeug("penecho_edit_canvas", {
                "sessionId": session_id,
                "requestId": request_id[:MAX_SCHLUESSEL],
                "action": "create_text",
                "text": text[:MAX_TITEL],
                "region": dict(rechteck),
            })
        except PenEchoPlatzBelegt:
            log.info("Namensschild steht schon da")

    def rueckmeldung_zeigen(
        self, session_id: str, aufgabe_id: str, nummer: int, *, gelesen: str | None, urteil: str, text: str,
        rechteck: dict[str, int] | None = None,
    ) -> None:
        """Vorbewertung neben die Aufgabe (`rueckmeldung_setzen`); jede neue ersetzt die vorige."""
        if rechteck is None:
            bereich(nummer)  # ausserhalb 1..20 ValueError, ohne PenEcho zu rufen
        if urteil not in URTEILE:
            raise ValueError("unbekanntes Urteil")
        self.rueckmeldung_setzen(session_id, aufgabe_id, nummer, f"Rückmeldung zu Aufgabe {nummer}",
                                 rueckmeldungstext(gelesen, urteil, text), rechteck=rechteck)

    def ergebnis_schnappschuss(self, session_id: str, nummer: int, *, rechteck: dict[str, int] | None = None) -> bytes:
        """PNG des Ergebnisfelds (nur die Handschrift der Lernenden; der Rahmen liegt ausserhalb)."""
        return self._aufnahme(session_id, felder(rechteck).ergebnisfeld if rechteck is not None else ergebnisfeld(nummer))

    def schnappschuss(self, session_id: str, nummer: int, *, rechteck: dict[str, int] | None = None,
                      timeout: float | None = None) -> bytes:
        return self._aufnahme(session_id, rechteck if rechteck is not None else bereich(nummer), timeout)

    # ---- Listenkurs: Markenzeile und Ansicht ---------------------------------
    def bereiche_melden(self, session_id: str, bereiche: Sequence[dict[str, int]]) -> None:
        """Markenzeile der Bereiche (`anordnung.markenzeile`) in die context.md des Boards.

        Vertrag mit der Lernflaeche: eine eigene Zeile `<!-- lernflaeche-bereiche: [...] -->`. Eine
        vorhandene Markenzeile wird ersetzt, sonst kommt sie ans Ende; der uebrige Inhalt bleibt.
        Steht sie schon so da, wird nichts geschrieben. PenEcho liefert context.md immer, leer als "".
        Hat sich die Datei zwischen Lesen und Schreiben geaendert (SOURCE_CONFLICT), einmal neu lesen.
        """
        neu = markenzeile(bereiche)
        for versuch in (1, 2):
            lesen = self._struktur(self._werkzeug("penecho_read_file", {"sessionId": session_id, "path": _KONTEXT}))
            inhalt, pruefsumme = lesen.get("content"), lesen.get("contentHash")
            if not isinstance(inhalt, str) or not isinstance(pruefsumme, str) or not pruefsumme:
                raise PenEchoNichtErreichbar("context")
            patch = _markenpatch(inhalt, neu)
            if patch is None:
                return
            kennung = hashlib.sha256(f"{pruefsumme}\n{patch}".encode("utf-8")).hexdigest()[:32]
            try:
                self._werkzeug("penecho_patch_file", {
                    "sessionId": session_id,
                    "path": _KONTEXT,
                    "contentHash": pruefsumme,
                    "patch": patch,
                    "requestId": f"bereiche-{kennung}",
                })
                return
            except PenEchoQuelleVeraltet:
                if versuch == 2:
                    raise

    def bereich_zeigen(self, session_id: str, rechteck: dict[str, int]) -> None:
        """Holt die Zeile des Bereichs (`anordnung.zeile`: Aufgabe samt Rueckmeldung) ins Bild.

        Die Lernflaeche nimmt bei show einen Bereich (schema.js EDIT_FIELDS.show) und rahmt ihn im Lernmodus
        ganz ein. Mit der objectId des Aufgabentexts lag das Antwortfeld halb ausserhalb (Live-Pruefung
        09.11.). Fehler wie bei `_zeigen`: nur verlorene Sitzung/Flaeche gehen weiter.
        """
        self._zeigen(session_id, region=zeile(rechteck))

    def _aufnahme(self, session_id: str, region: dict[str, int], timeout: float | None = None) -> bytes:
        # schema.js:666 penecho_capture_canvas: target "region" verlangt region {x,y,w,h};
        # das Bild kommt als content-Eintrag type "image" (base64 in data), protocol.js:28-37.
        grenze = {} if timeout is None else {"timeout": timeout}
        ergebnis = self._werkzeug("penecho_capture_canvas", {
            "sessionId": session_id,
            "target": "region",
            "region": region,
            "quality": "detail",
        }, **grenze)
        for teil in ergebnis.get("content") or []:
            if isinstance(teil, dict) and teil.get("type") == "image" and isinstance(teil.get("data"), str):
                try:
                    return base64.b64decode(teil["data"], validate=True)
                except ValueError:
                    break
        raise PenEchoNichtErreichbar("bild")
