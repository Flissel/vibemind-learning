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
from typing import Any
from uuid import uuid4

import httpx

from .tafel import rueckmeldungstext

log = logging.getLogger(__name__)

PROTOKOLL = "2025-11-25"
SCHLUESSEL_PRAEFIX = "learning-huelle:"
MAX_SCHLUESSEL = 128  # schema.js: sessionKey/sessionId/requestId maxLength 128

# Ruling L: eine Flaeche je Lernsitzung mit 20 Aufgabenbereichen in PenEchos Weltgrenzen
# (schema.js: Weltkoordinaten 0..20000). Zwei Spalten zu zehn Zeilen; Aufgabe 1-10 links, 11-20 rechts.
FLAECHE_GROESSE = 20000
BEREICHE = 20
BEREICH_BREITE = 9000
BEREICH_HOEHE = 1900
_RAND = 500
_SPALTEN_SCHRITT = 10000
_ZEILEN_SCHRITT = 1950
_ZEILEN = 10

# Ergebnisfeld (Task 6): unteres rechtes Viertel des Bereichs. Rahmen und Schild liegen AUSSERHALB
# des Feldes, damit der Schnappschuss des Feldes nur die Handschrift der Lernenden enthaelt.
FELD_BREITE = 2000
FELD_HOEHE = 700
_FELD_RAND_RECHTS = 150
_FELD_RAND_UNTEN = 100
_RAHMEN_ABSTAND = 8
_RAHMEN_STAERKE = 4
_RAHMEN_FARBE = "#2b6cb0"
# "Wird noch geprüft.": Learnings Zustand needs_review (Rubrik-Bewertung nicht angenommen).
URTEILE = ("Richtig.", "Teilweise richtig.", "Noch nicht richtig.", "Nicht lesbar.", "Wird noch geprüft.")
_SCHILD_HOEHE = 56  # Abstand der Schild-Oberkante ueber dem Feld

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
    spalte, zeile = divmod(nummer - 1, _ZEILEN)
    return {
        "x": _RAND + spalte * _SPALTEN_SCHRITT,
        "y": _RAND + zeile * _ZEILEN_SCHRITT,
        "w": BEREICH_BREITE,
        "h": BEREICH_HOEHE,
    }


def ergebnisfeld(nummer: int) -> dict[str, int]:
    """Weltrechteck des Ergebnisfelds im unteren rechten Viertel von `bereich(nummer)` (>= 360x140)."""
    b = bereich(nummer)
    return {
        "x": b["x"] + b["w"] - _FELD_RAND_RECHTS - FELD_BREITE,
        "y": b["y"] + b["h"] - _FELD_RAND_UNTEN - FELD_HOEHE,
        "w": FELD_BREITE,
        "h": FELD_HOEHE,
    }


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
    """Der gewuenschte Bereich ist schon belegt (PenEcho LAYOUT_CONFLICT)."""


class PenEchoRevisionVeraltet(PenEchoNichtErreichbar):
    """PenEcho meldet REVISION_CONFLICT: die Flaeche hat sich seit dem Lesen der Revision geaendert."""


class PenEchoAnfrageSchonBekannt(PenEchoNichtErreichbar):
    """PenEcho meldet REQUEST_ID_CONFLICT: die requestId wurde mit anderen Argumenten schon benutzt."""


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
            code = self._struktur(ergebnis).get("code")
            # Nur PenEchos feste Codes (z. B. LAYOUT_CONFLICT) ins Log, nie Meldungstext.
            sicher = code if isinstance(code, str) and _CODE_MUSTER.fullmatch(code) else "?"
            log.warning("PenEcho-Werkzeug %s meldet Fehler %s", name, sicher)
            if isinstance(code, str) and code in _SITZUNG_CODES:
                raise PenEchoSitzungUnbekannt(code)
            if isinstance(code, str) and code in _VERLOREN_CODES:
                raise PenEchoFlaecheVerloren(code)
            if code == "LAYOUT_CONFLICT":
                raise PenEchoPlatzBelegt(code)
            if code == "REVISION_CONFLICT":
                raise PenEchoRevisionVeraltet(code)
            if code == "REQUEST_ID_CONFLICT":
                raise PenEchoAnfrageSchonBekannt(code)
            raise PenEchoNichtErreichbar("werkzeug")
        return ergebnis

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
        self, session_id: str, aufgabe_id: str, text: str, nummer: int, *, mit_ergebnisfeld: bool = False
    ) -> None:
        # schema.js:665 penecho_edit_canvas: action create_text braucht text, optional region (x/y).
        # Feste requestId: dieselbe Aufgabe am selben Platz wiederholt PenEcho nicht (Idempotenz).
        region = bereich(nummer)
        argumente = {
            "sessionId": session_id,
            "requestId": f"aufgabe-{aufgabe_id}-{nummer}"[:MAX_SCHLUESSEL],
            "action": "create_text",
            "text": f"Aufgabe {nummer}: {text}",
            "region": region,
        }
        try:
            self._werkzeug("penecho_edit_canvas", argumente)
        except PenEchoPlatzBelegt:
            # PenEchos KI hat den Bereich nach einer Abgabe belegt: PenEcho einen freien Platz waehlen lassen.
            del argumente["region"]
            argumente["requestId"] = f"aufgabe-{aufgabe_id}-{nummer}-frei"[:MAX_SCHLUESSEL]
            self._werkzeug("penecho_edit_canvas", argumente)
        if mit_ergebnisfeld:
            self._ergebnisfeld_zeichnen(session_id, aufgabe_id, nummer)

    @staticmethod
    def _ergebnis_id(aufgabe_id: str, nummer: int, zusatz: str = "") -> str:
        # Der Zusatz bleibt immer erhalten, nur die aufgabe_id wird bei Bedarf gekuerzt.
        rest = MAX_SCHLUESSEL - len(f"ergebnis--{nummer}{zusatz}")
        return f"ergebnis-{aufgabe_id[:rest]}-{nummer}{zusatz}"

    def _ergebnisfeld_zeichnen(self, session_id: str, aufgabe_id: str, nummer: int) -> None:
        """Schild "Ihr Ergebnis zu Aufgabe N" ueber dem Feld und ein Tintenrahmen um das Feld.

        Der Rahmen ist Tinte (penecho_edit_canvas draw_ink), kein Objekt: Tinte ist nur Pixel in der
        Zeichenebene, faengt also keine Stifteingabe ab, und die Lernenden schreiben ungehindert
        darueber. penecho_draw/Widgets sind eigene Objekte und wuerden Eingaben aufnehmen; zudem
        platziert der Host sie selbst, nicht an Weltkoordinaten. Rahmen und Schild sind Zierde: Fehler
        ausser verlorener Sitzung/Flaeche brechen die Aufgabe nicht ab (der Schnappschuss gilt
        ohnehin dem festen Feld).
        """
        feld = ergebnisfeld(nummer)
        try:
            self._werkzeug("penecho_edit_canvas", {
                "sessionId": session_id,
                "requestId": self._ergebnis_id(aufgabe_id, nummer, "-schild"),
                "action": "create_text",
                # Mit Nummer: PenEcho verschiebt Aufgabentexte, die Felder bleiben im festen Raster.
                "text": f"Ihr Ergebnis zu Aufgabe {nummer}",
                "region": {"x": feld["x"], "y": feld["y"] - _SCHILD_HOEHE, "w": 360, "h": 40},
            })
        except (PenEchoSitzungUnbekannt, PenEchoFlaecheVerloren):
            raise
        except PenEchoNichtErreichbar as fehler:  # u. a. PenEchoPlatzBelegt: Schild fehlt, Aufgabe laeuft weiter
            log.warning("Ergebnisfeld-Schild nicht gesetzt: %s", _typ(fehler))
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
                    "requestId": self._ergebnis_id(aufgabe_id, nummer, "" if versuch == 1 else "-r2"),
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
                log.warning("Ergebnisfeld-Rahmen nicht gezeichnet: REQUEST_ID_CONFLICT")
                return
            except PenEchoRevisionVeraltet:
                if versuch == 1:
                    continue  # Flaeche hat sich geaendert: Revision neu lesen, einmal wiederholen
                log.warning("Ergebnisfeld-Rahmen nicht gezeichnet: Revision veraltet")
            except (PenEchoSitzungUnbekannt, PenEchoFlaecheVerloren):
                raise
            except PenEchoNichtErreichbar as fehler:
                log.warning("Ergebnisfeld-Rahmen nicht gezeichnet: %s", _typ(fehler))
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

    def notizkarte(self, session_id: str, aufgabe_id: str, titel: str, text: str) -> None:
        # Klein (480x360): in Seitengroesse (Vorgabe 1200x800) fand PenEcho ab Aufgabe 4 keinen Platz mehr.
        self._karte(session_id, f"rueckmeldung-{aufgabe_id}", titel, text, groesse="base")

    def _karte(self, session_id: str, artifact_id: str, titel: str, text: str, *, groesse: str | None = None) -> None:
        # schema.js:675-677 penecho_present_widget: sessionId, artifactId, title + genau eines
        # aus html/architecture/sequence/workflow/scene/note
        # schema.js:703: requestId ist Pflicht und "nur fuer identische Argumente wiederverwenden";
        # daher traegt sie einen Kurz-Hash der Argumente (deterministisch, <= 128 Zeichen).
        signatur = f"{titel}\n{text}" + (f"\n{groesse}" if groesse is not None else "")
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
        if groesse is not None:
            # schema.js presentationSchema: size base 480x360 ... page 1200x800 (Vorgabe).
            argumente["presentation"] = {"size": groesse}
        self._werkzeug("penecho_present_widget", argumente)

    def bericht_zeigen(self, session_id: str, sid: str, text: str, nummern: Sequence[int] = ()) -> None:
        """Berichtskarte `bericht-{sid}` und, je Aufgabe, der Schnappschuss ihres Bereichs.

        Wiederholbar: die Karte hat eine feste artifactId, jedes Bild eine feste requestId
        `bericht-{sid}-{nummer}`. Ein Bild, das nicht passt (zu gross, schon gesetzt, PenEcho lehnt ab),
        faellt weg; die Karte bleibt. Die Bilder setzt PenEcho selbst (ohne region, Auto-Layout), sie
        liegen also nicht garantiert unter der Karte. Der erste Transportfehler beendet die Bildschleife,
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
        for position, nummer in enumerate(nummern):
            if _uhr() >= ende:
                log.warning("Berichtsbilder: Zeitbudget erschoepft, %d uebersprungen", len(nummern) - position)
                break
            try:
                quelle = _datenurl(self.schnappschuss(session_id, nummer))
                if quelle is None or len(quelle) > MAX_BILD_DATENURL:
                    log.warning("Berichtsbild uebersprungen: Format oder Groesse")
                    continue
                self._werkzeug("penecho_place_image", {
                    "sessionId": session_id,
                    "requestId": f"bericht-{sid}-{nummer}"[:MAX_SCHLUESSEL],
                    "source": quelle,
                    "width": BERICHT_BILD_BREITE,
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
        """Rueckmeldungskarte zur Aufgabe; gleiche artifactId, jede neue Vorbewertung ersetzt sie.

        PenEchos Host platziert Widgets selbst (presentation.relativeTo verlangt eine artifactId, die
        Aufgabentext und Ergebnisfeld nicht haben); `nummer` prueft nur den Bereich.
        """
        bereich(nummer)  # ausserhalb 1..20 ValueError, ohne PenEcho zu rufen
        if urteil not in URTEILE:
            raise ValueError("unbekanntes Urteil")
        self.notizkarte(session_id, aufgabe_id, "Rückmeldung", rueckmeldungstext(gelesen, urteil, text))

    def ergebnis_schnappschuss(self, session_id: str, nummer: int) -> bytes:
        """PNG des Ergebnisfelds (nur die Handschrift der Lernenden; Rahmen und Schild liegen ausserhalb)."""
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
