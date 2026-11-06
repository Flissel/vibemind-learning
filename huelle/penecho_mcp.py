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
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

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

    def speichern(self, session_id: str) -> None:
        """Sichert das Board der Sitzung als PenEcho-Dokument (BOARD_SPEICHERN.md). Wiederholbar.

        PenEcho schreibt jedes MCP-Board laufend selbst in die Arbeitsflaeche des Browsers
        (IndexedDB); ein MCP-Werkzeug zum Speichern an einen Ablageort gibt es in PenEcho 1.4.2
        nicht. speichern() fragt PenEcho nach dem Dokument der Sitzung (schema.js
        penecho_inspect_session: nur sessionId) und merkt dessen ID, damit sitzung() genau dieses
        Board wieder oeffnet. Fehler kommen als PenEchoNichtErreichbar.
        """
        ergebnis = self._werkzeug("penecho_inspect_session", {"sessionId": session_id})
        dokument = self._struktur(ergebnis).get("documentId")
        if not isinstance(dokument, str) or not dokument:
            raise PenEchoNichtErreichbar("documentId")
        dokumente = self._gemerkt(self._dokumentdatei)
        for schluessel, sid in self._gemerkt().items():
            if sid == session_id:
                self._dokument_merken(schluessel, dokument, dokumente)

    def aufgabe_setzen(self, session_id: str, aufgabe_id: str, text: str, nummer: int) -> None:
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

    def notizkarte(self, session_id: str, aufgabe_id: str, titel: str, text: str) -> None:
        # schema.js:675-677 penecho_present_widget: sessionId, artifactId, title + genau eines
        # aus html/architecture/sequence/workflow/scene/note
        # schema.js:703: requestId ist Pflicht und "nur fuer identische Argumente wiederverwenden";
        # daher traegt sie einen Kurz-Hash der Argumente (deterministisch, <= 128 Zeichen).
        pruefsumme = hashlib.sha256(f"{titel}\n{text}".encode("utf-8")).hexdigest()[:12]
        rest = MAX_SCHLUESSEL - len("rueckmeldung--") - len(pruefsumme)
        self._werkzeug("penecho_present_widget", {
            "sessionId": session_id,
            "requestId": f"rueckmeldung-{aufgabe_id[:rest]}-{pruefsumme}",
            "artifactId": f"rueckmeldung-{aufgabe_id}"[:MAX_SCHLUESSEL],
            "title": titel,
            "note": {
                "title": titel,
                "style": "note",
                "summary": text,
                "blocks": [{"type": "markdown", "text": text}],
            },
        })

    def schnappschuss(self, session_id: str, nummer: int) -> bytes:
        # schema.js:666 penecho_capture_canvas: target "region" verlangt region {x,y,w,h};
        # das Bild kommt als content-Eintrag type "image" (base64 in data), protocol.js:28-37.
        region = bereich(nummer)
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
