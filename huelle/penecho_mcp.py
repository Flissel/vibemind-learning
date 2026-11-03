"""MCP-Client der Huelle fuer PenEcho (JSON-RPC ueber HTTPS, gepinntes Zertifikat).

Argumentformen: node_modules/penecho/src/server/mcp/schema.js (PenEcho 1.4.2).
Der Bearer-Token wird bei jedem Verbindungsaufbau aus identity.json gelesen und
nie geloggt; Fehler werden nur als Typname gemeldet.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

RASTER_HOEHE = 1600
RASTER_BREITE = 2400
PROTOKOLL = "2025-11-25"


class PenEchoNichtErreichbar(Exception):
    """PenEcho antwortet nicht oder lehnt die Anfrage ab."""


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
            raise PenEchoNichtErreichbar(_typ(fehler)) from None
        if not isinstance(daten, dict) or "error" in daten or not isinstance(daten.get("result"), dict):
            log.warning("PenEcho-Aufruf %s abgelehnt", methode)
            raise PenEchoNichtErreichbar("jsonrpc")
        return daten["result"]

    def _werkzeug(self, name: str, argumente: dict[str, Any]) -> dict[str, Any]:
        self._verbinden()
        try:
            ergebnis = self._rpc("tools/call", {"name": name, "arguments": argumente})
        except PenEchoNichtErreichbar:
            self._zuruecksetzen()  # naechster Aufruf baut neu auf (z. B. nach PenEcho-Neustart)
            raise
        if ergebnis.get("isError"):
            log.warning("PenEcho-Werkzeug %s meldet Fehler", name)
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
    def _gemerkt(self) -> dict[str, str]:
        try:
            wert = json.loads(self._merkdatei.read_text(encoding="utf-8"))
            return {k: v for k, v in wert.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _merken(self, karte: dict[str, str]) -> None:
        self._merkdatei.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._merkdatei.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(karte, f, ensure_ascii=False)
            os.replace(tmp, self._merkdatei)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- Oeffentliche Methoden -------------------------------------------
    def sitzung(self, titel: str) -> str:
        # schema.js:656-658 penecho_start_session: title (Pflicht), sessionKey, restore (bool)
        # Die Merkdatei haelt titel -> sessionId; ein bekannter Titel startet keine neue Sitzung.
        bekannt = self._gemerkt().get(titel)
        if bekannt:
            return bekannt
        ergebnis = self._werkzeug("penecho_start_session", {
            "title": titel,
            "sessionKey": "learning-huelle:" + titel,
            "restore": True,
        })
        session_id = self._struktur(ergebnis).get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise PenEchoNichtErreichbar("sessionId")
        karte = self._gemerkt()
        karte[titel] = session_id
        self._merken(karte)
        return session_id

    def aufgabe_setzen(self, session_id: str, aufgabe_id: str, text: str, nummer: int) -> None:
        # schema.js:665 penecho_edit_canvas: action create_text braucht text, optional region (x/y)
        self._werkzeug("penecho_edit_canvas", {
            "sessionId": session_id,
            "requestId": f"aufgabe-{aufgabe_id}-{uuid.uuid4().hex[:8]}",
            "action": "create_text",
            "text": f"Aufgabe {nummer}: {text}",
            "region": {"x": 0, "y": nummer * RASTER_HOEHE, "w": RASTER_BREITE, "h": RASTER_HOEHE},
        })

    def notizkarte(self, session_id: str, aufgabe_id: str, titel: str, text: str) -> None:
        # schema.js:675-677 penecho_present_widget: sessionId, artifactId, title + genau eines
        # aus html/architecture/sequence/workflow/scene/note
        self._werkzeug("penecho_present_widget", {
            "sessionId": session_id,
            "artifactId": f"rueckmeldung-{aufgabe_id}",
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
        ergebnis = self._werkzeug("penecho_capture_canvas", {
            "sessionId": session_id,
            "target": "region",
            "region": {"x": 0, "y": nummer * RASTER_HOEHE, "w": RASTER_BREITE, "h": RASTER_HOEHE},
            "quality": "detail",
        })
        for teil in ergebnis.get("content") or []:
            if isinstance(teil, dict) and teil.get("type") == "image" and isinstance(teil.get("data"), str):
                try:
                    return base64.b64decode(teil["data"], validate=True)
                except ValueError:
                    break
        raise PenEchoNichtErreichbar("bild")
