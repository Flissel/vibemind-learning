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
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

import httpx

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
_RUECKMELDUNG_BREITE = 420
# Abnahme 06.11. (F14): PenEcho rendert hoechstens 2000 Zeichen (core.js TEXT_INPUT_MAX_LENGTH, sonst
# INVALID_TEXT), Schrift 20 px mit Zeilenhoehe 1,35 (27 px), Umbruch bei 400 px. Ein laengerer Text ragte ueber
# das Rueckmeldungsfeld in den naechsten Bereich. 36 Zeichen je Zeile ist vorsichtig geschaetzt.
_TEXT_HOECHSTENS = 2000
_RUECKMELDUNG_ZEILEN = (BEREICH_HOEHE - 2 * _TEXT_RAND) // 27
_ZEICHEN_JE_ZEILE = 36

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
BERICHT_BILD_BREITE = 900
BERICHT_BILD_ZEIT = 20.0  # Sekunden fuer alle Berichtsbilder zusammen; danach entfallen die uebrigen
MAX_TITEL = 120  # schema.js MAX_TITLE_CHARS
_uhr = time.monotonic  # nur fuer Tests ersetzbar


def bereich(nummer: int) -> dict[str, int]:
    """Weltregion des Aufgabenbereichs `nummer` (1..20); ausserhalb ValueError."""
    if isinstance(nummer, bool) or not isinstance(nummer, int) or not 1 <= nummer <= BEREICHE:
        raise ValueError("Aufgabenbereich ausserhalb der Flaeche")
    return {"x": _RAND, "y": _RAND + (nummer - 1) * _ZEILEN_SCHRITT, "w": BEREICH_BREITE, "h": BEREICH_HOEHE}


def textfeld(nummer: int) -> dict[str, int]:
    """Platz des Aufgabentexts oben links in `bereich(nummer)`, links neben dem Ergebnisfeld."""
    b = bereich(nummer)
    return {"x": b["x"] + _TEXT_RAND, "y": b["y"] + _TEXT_RAND, "w": _TEXT_BREITE, "h": b["h"] - 2 * _TEXT_RAND}


def ergebnisfeld(nummer: int) -> dict[str, int]:
    """Weltrechteck des Ergebnisfelds oben rechts in `bereich(nummer)` (>= 360x140)."""
    b = bereich(nummer)
    return {
        "x": b["x"] + b["w"] - _FELD_RAND_RECHTS - FELD_BREITE,
        "y": b["y"] + _FELD_RAND_OBEN,
        "w": FELD_BREITE,
        "h": FELD_HOEHE,
    }


def arbeitsfeld(nummer: int) -> dict[str, int]:
    """Platz fuer Skizze oder freie Antwort (offene/Zeichenaufgaben): rechts neben dem Text, bis unten.

    Nur eine sichtbare Grenze; bewertet wird weiter der ganze Bereich (`schnappschuss`).
    """
    b, t = bereich(nummer), textfeld(nummer)
    x = t["x"] + t["w"] + _TEXT_RAND
    return {"x": x, "y": b["y"] + _FELD_RAND_OBEN, "w": b["x"] + b["w"] - _FELD_RAND_RECHTS - x,
            "h": b["h"] - _FELD_RAND_OBEN - _TEXT_RAND}


_BILD_LUECKE = 20
_BILD_SCHILD = 40  # Hoehe der Beschriftung ueber einem Bild (create_text, Schrift 20)
_BILD_MIN = 80  # schema.js penecho_place_image: width/height minimum 80


class Aufgabenbild(NamedTuple):
    """Ein Bild der Aufgabe (Graph, Skizze) mit optionaler Beschriftung; `seitenverhaeltnis` = Breite/Hoehe."""

    beschriftung: str | None
    daten: bytes
    seitenverhaeltnis: float


def bildfeld(nummer: int, *, unter_ergebnisfeld: bool) -> dict[str, int]:
    """Platz der Aufgabenbilder rechts neben dem Text.

    Mit Ergebnisfeld darunter, bis zum Bereichsende; sonst oben im Arbeitsfeld, damit darunter Platz zum
    Schreiben bleibt und offene Aufgaben direkt im Bild beschriftet werden koennen.
    """
    a = arbeitsfeld(nummer)
    if unter_ergebnisfeld:
        e = ergebnisfeld(nummer)
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
    b = bereich(nummer)
    return {"x": b["x"] + b["w"] + _RUECKMELDUNG_ABSTAND, "y": b["y"] + _TEXT_RAND,
            "w": _RUECKMELDUNG_BREITE, "h": b["h"] - 2 * _TEXT_RAND}


_BERICHT_ABSTAND = 80


def berichtsplatz(nummer: int) -> dict[str, int]:
    """Platz des Berichtsbilds (Schnappschuss) zu Aufgabe `nummer`: rechts neben ihrem Rueckmeldungsfeld.

    Abnahme 06.11. (F19): mit fester region setzt PenEcho das Bild im Massstab 1; ohne region rechnete es
    die Breite durch den Zoom der Ansicht und lehnte bei kleinem Zoom ab (INVALID_GEOMETRY).
    Ein Schnappschuss des Bereichs (1300 x 800) ist bei Breite 900 rund 554 hoch und passt in die Zeile.
    """
    r = rueckmeldungsfeld(nummer)
    return {"x": r["x"] + r["w"] + _BERICHT_ABSTAND, "y": bereich(nummer)["y"],
            "w": BERICHT_BILD_BREITE, "h": BEREICH_HOEHE}


def _ueberlappt(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


def _sitzungsschluessel(schluessel: str) -> str:
    roh = SCHLUESSEL_PRAEFIX + schluessel
    if len(roh) <= MAX_SCHLUESSEL:
        return roh
    return SCHLUESSEL_PRAEFIX + hashlib.sha256(schluessel.encode("utf-8")).hexdigest()


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

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        assert self._client is not None
        kopf = {"Content-Type": "application/json"}
        if self._mcp_session:
            kopf["Mcp-Session-Id"] = self._mcp_session
            kopf["MCP-Protocol-Version"] = PROTOKOLL
        antwort = self._client.post(self._url, json=body, headers=kopf)
        antwort.raise_for_status()
        return antwort

    def _rpc(self, methode: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        try:
            antwort = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": methode, "params": params})
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

    def _werkzeug(self, name: str, argumente: dict[str, Any]) -> dict[str, Any]:
        for versuch in (1, 2):
            self._verbinden()
            try:
                ergebnis = self._rpc("tools/call", {"name": name, "arguments": argumente})
            except _HttpFehler as fehler:
                # Der MCP-Zugang verfaellt nach Leerlauf (404/400): neu verbinden, einmal wiederholen.
                self._zuruecksetzen()
                if versuch == 1 and fehler.status in (400, 404):
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

    # ---- Oeffentliche Methoden -------------------------------------------
    def sitzung(self, schluessel: str, titel: str) -> str:
        """PenEcho-Sitzung (eine Flaeche) zu `schluessel`, der Learning-Sitzungs-ID (Ruling L).

        schema.js:656-658 penecho_start_session: title (Pflicht), sessionKey, restore (bool).
        Die Merkdatei haelt schluessel -> sessionId; ein bekannter Schluessel startet keine neue Sitzung.
        Der Titel zaehlt nur beim ersten Anlegen.
        Nach einem Neustart findet PenEcho das Board ueber den sessionKey wieder. Meldet PenEcho die
        Flaeche als verloren, wird das gemerkte Dokument (dokumente.json) in jedem verbundenen Browser
        gesucht; erst wenn es nirgends liegt, entsteht eine neue Flaeche unter neuem Schluessel.
        """
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
        mit_arbeitsfeld: bool = False, bilder: Sequence[Aufgabenbild] = (),
    ) -> None:
        # schema.js:665 penecho_edit_canvas: action create_text braucht text, optional region (x/y).
        # Feste requestId: dieselbe Aufgabe am selben Platz wiederholt PenEcho nicht (Idempotenz).
        region = textfeld(nummer)
        hinweis = _HINWEIS_ERGEBNIS if mit_ergebnisfeld else _HINWEIS_LOESUNG if mit_arbeitsfeld else None
        argumente = {
            "sessionId": session_id,
            "requestId": f"aufgabe-{aufgabe_id}-{nummer}"[:MAX_SCHLUESSEL],
            "action": "create_text",
            "text": f"Aufgabe {nummer}: {text}" + (f"\n\n{hinweis}" if hinweis else ""),
            "region": region,
        }
        voll = False
        belegung = self._belegung(session_id, bereich(nummer))
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
        self._zeigen(session_id, objekt_id)
        if bilder:
            # Vor dem Rahmen: dessen Tinte zaehlte sonst als Hindernis im Bildfeld.
            # Volle Flaeche: keine Beschriftungen versuchen. Jeder gescheiterte Text bliebe in PenEchos
            # Sitzung unaufgeloest und sperrte nach vier weiteren Fehlern alle Aenderungen (F17).
            self._bilder_setzen(session_id, aufgabe_id, nummer, bilder,
                                bildfeld(nummer, unter_ergebnisfeld=mit_ergebnisfeld), beschriften=not voll,
                                belegung=belegung)
        if mit_ergebnisfeld:
            self._feld_zeichnen(session_id, aufgabe_id, nummer, ergebnisfeld(nummer), "ergebnis")
        elif mit_arbeitsfeld:
            self._feld_zeichnen(session_id, aufgabe_id, nummer, arbeitsfeld(nummer), "arbeit")

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

    def _zeigen(self, session_id: str, objekt_id: object = None) -> None:
        """Board sichtbar machen und die Aufgabe ins Bild holen (show). Ab Aufgabe 2 laege sie sonst
        unter dem Bildschirmrand; ein frisch angelegtes Board ist ohne show nicht aktiv.

        Zierde wie der Rahmen: nur verlorene Sitzung/Flaeche gehen an den Aufrufer. Eine neue requestId je
        Aufruf, sonst spielte PenEcho beim erneuten Oeffnen nur die alte Antwort ab und zeigte nichts.
        """
        argumente: dict[str, Any] = {"sessionId": session_id, "requestId": f"zeigen-{uuid4().hex}", "action": "show"}
        if isinstance(objekt_id, str) and objekt_id:
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

    def _feld_zeichnen(self, session_id: str, aufgabe_id: str, nummer: int, feld: dict[str, int], praefix: str) -> None:
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
                if versuch == 1:
                    self._zeigen(session_id)  # Board inzwischen verdeckt: zeigen, einmal wiederholen
                    continue
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

    def rueckmeldung_setzen(self, session_id: str, aufgabe_id: str, nummer: int, titel: str, text: str) -> None:
        """Rueckmeldung als Text in `rueckmeldungsfeld(nummer)`; eine neue ersetzt die alte.

        Kein Widget mehr: PenEcho platzierte die Karten selbst und belegte damit die Bereiche der
        naechsten Aufgaben. Liegt am Platz schon eine fruehere Rueckmeldung (ein Text exakt an der
        Feldecke), wird sie geloescht und einmal neu gesetzt. Jedes andere Hindernis (Handschrift,
        Widget, Text der Lernenden) bleibt; dann PenEchoPlatzBelegt, die Bruecke zeigt die Leiste.
        """
        feld = rueckmeldungsfeld(nummer)  # ausserhalb 1..20 ValueError, ohne PenEcho zu rufen
        # Zu lang fuer das Feld: gekuerzt; die volle Rueckmeldung bleibt in Learning (Bericht).
        inhalt = kuerzen(f"{titel}\n\n{text}", zeichen=_TEXT_HOECHSTENS, zeilen=_RUECKMELDUNG_ZEILEN,
                         je_zeile=_ZEICHEN_JE_ZEILE)
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

    def bericht_zeigen(self, session_id: str, sid: str, text: str, nummern: Sequence[int] = ()) -> None:
        """Berichtskarte `bericht-{sid}` und, je Aufgabe, der Schnappschuss ihres Bereichs.

        Wiederholbar: die Karte hat eine feste artifactId, jedes Bild eine feste requestId
        `bericht-{sid}-{nummer}`. Ein Bild, das nicht passt (zu gross, schon gesetzt, PenEcho lehnt ab),
        faellt weg; die Karte bleibt. Jedes Bild steht an `berichtsplatz(nummer)` neben seiner Aufgabe;
        liegt dort laut Belegung schon etwas (das Bild selbst oder ein fremdes Objekt), wird es weder
        aufgenommen noch gesendet. Der erste Transportfehler beendet die Bildschleife,
        ebenso ein Zeitbudget von `BERICHT_BILD_ZEIT` fuer alle Bilder; die Karte steht dann schon.
        Nur verlorene Sitzung/Flaeche gehen an den Aufrufer.
        """
        erste = text.splitlines()[0] if text.strip() else ""
        titel = re.sub(r"\\(.)", r"\1", erste.lstrip("# ").strip()) or "Bericht"
        if len(titel) > MAX_TITEL:
            titel = titel[: MAX_TITEL - 1].rstrip() + "\u2026"
        self._karte(session_id, f"bericht-{sid}", titel, text)
        if not BERICHT_MIT_SCHNAPPSCHUESSEN:
            return
        ende = _uhr() + BERICHT_BILD_ZEIT
        spalte = berichtsplatz(1)
        belegung = self._belegung(session_id, {**spalte, "h": BEREICHE * _ZEILEN_SCHRITT})
        for position, nummer in enumerate(nummern):
            if _uhr() >= ende:
                log.warning("Berichtsbilder: Zeitbudget erschoepft, %d uebersprungen", len(nummern) - position)
                break
            try:
                platz = berichtsplatz(nummer)
                if any(_ueberlappt(e["bounds"], platz) for e in belegung):
                    log.info("Berichtsbild %d: Platz schon belegt", nummer)
                    continue
                quelle = _datenurl(self.schnappschuss(session_id, nummer))
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

    def rueckmeldung_zeigen(
        self, session_id: str, aufgabe_id: str, nummer: int, *, gelesen: str | None, urteil: str, text: str
    ) -> None:
        """Vorbewertung neben die Aufgabe (`rueckmeldung_setzen`); jede neue ersetzt die vorige."""
        bereich(nummer)  # ausserhalb 1..20 ValueError, ohne PenEcho zu rufen
        if urteil not in URTEILE:
            raise ValueError("unbekanntes Urteil")
        self.rueckmeldung_setzen(session_id, aufgabe_id, nummer, f"Rückmeldung zu Aufgabe {nummer}",
                                 rueckmeldungstext(gelesen, urteil, text))

    def ergebnis_schnappschuss(self, session_id: str, nummer: int) -> bytes:
        """PNG des Ergebnisfelds (nur die Handschrift der Lernenden; der Rahmen liegt ausserhalb)."""
        return self._aufnahme(session_id, ergebnisfeld(nummer))

    def schnappschuss(self, session_id: str, nummer: int) -> bytes:
        return self._aufnahme(session_id, bereich(nummer))

    def _aufnahme(self, session_id: str, region: dict[str, int]) -> bytes:
        # schema.js:666 penecho_capture_canvas: target "region" verlangt region {x,y,w,h};
        # das Bild kommt als content-Eintrag type "image" (base64 in data), protocol.js:28-37.
        ergebnis = self._werkzeug("penecho_capture_canvas", {
            "sessionId": session_id,
            "target": "region",
            "region": region,
            "quality": "detail",
        })
        for teil in ergebnis.get("content") or []:
            if isinstance(teil, dict) and teil.get("type") == "image" and isinstance(teil.get("data"), str):
                try:
                    return base64.b64decode(teil["data"], validate=True)
                except ValueError:
                    break
        raise PenEchoNichtErreichbar("bild")
