"""Durchreicher: ASGI-Reverse-Proxy auf PenEcho (gleiche Herkunft, einbettbar), mit KI-Sperre."""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import websockets

from .sperre import KI_PFADE, Sperre

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

SPERR_TEXT = "Während der Aufgabe hilft „Erklär mir das“."
NICHT_ERREICHBAR = "Die Fläche ist gerade nicht erreichbar."

_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})
# Vom Websocket-Handshake selbst erzeugt, nicht weiterreichen.
_WS_KOPF = frozenset({
    "host", "origin", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept",
})
_FRAME = re.compile(r"frame-ancestors\s+'none'", re.IGNORECASE)


_VERBOTEN = (chr(92).encode(), bytes([9]), bytes([13]), bytes([10]))  # Backslash, Tab, CR, LF


def pfad_unzulaessig(scope: Scope) -> bool:
    """Node (WHATWG-URL) macht aus Backslash ein `/` und streicht Tab/CR/LF; Browser senden das nie.

    Solche Pfade werden abgewiesen, statt sie zu kanonisieren, damit Pruefung und Weitergabe nie auseinanderlaufen.
    """
    roh = scope.get("raw_path") or scope["path"].encode("utf-8")
    return any(z in roh for z in _VERBOTEN)


def ist_ki_pfad(pfad: str) -> bool:
    """Prueft roh und normalisiert (Node loest `..` und %2e auf), ohne Gross-/Kleinschreibung."""
    pfad = pfad.replace("\\", "/")
    kandidaten = {pfad, unquote(pfad)}
    for k in list(kandidaten):
        kandidaten.add(posixpath.normpath(k))
        kandidaten.add(posixpath.normpath("/" + k.lstrip("/")))
    praefixe = tuple(p.casefold() for p in KI_PFADE)
    return any(k.casefold().startswith(praefixe) for k in kandidaten)


def _pfad_und_query(scope: Scope) -> str:
    pfad = (scope.get("raw_path") or scope["path"].encode("utf-8")).decode("latin-1")
    if scope.get("query_string"):
        pfad += "?" + scope["query_string"].decode("latin-1")
    return pfad


class Durchreicher:
    def __init__(self, penecho_url: str, sperre: Sperre, eigene_herkuenfte: frozenset[str]) -> None:
        teile = urlsplit(penecho_url)
        self._basis = f"{teile.scheme}://{teile.netloc}"
        self._netloc = teile.netloc
        self._ws_basis = ("wss" if teile.scheme == "https" else "ws") + f"://{teile.netloc}"
        self._sperre = sperre
        self._eigene = frozenset(h.lower() for h in eigene_herkuenfte)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._ws(scope, receive, send)
        elif scope["type"] == "lifespan":
            while True:
                nachricht = await receive()
                if nachricht["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif nachricht["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

    # --- HTTP ---------------------------------------------------------------------------

    def _origin_erlaubt(self, origin: str) -> bool:
        """Exakter Vergleich (scheme://host[:port], Host klein); `null` und Pfadanhaenge fallen durch."""
        return origin.lower() in self._eigene

    def _anfrage_kopf(self, scope: Scope) -> list[tuple[str, str]]:
        kopf: list[tuple[str, str]] = []
        hat_origin = False
        for roh_name, roh_wert in scope["headers"]:
            name, wert = roh_name.decode("latin-1").lower(), roh_wert.decode("latin-1")
            if name == "origin":
                hat_origin = True
            if name in _HOP or name in ("host", "origin"):
                continue
            kopf.append((name, wert))
        kopf.append(("host", self._netloc))
        if hat_origin:
            kopf.append(("origin", self._basis))
        return kopf

    @staticmethod
    def _antwort_kopf(roh: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        aus: list[tuple[bytes, bytes]] = []
        for name, wert in roh:
            if name.decode("latin-1").lower() in _HOP:
                continue
            if name.lower() == b"content-security-policy":
                wert = _FRAME.sub("frame-ancestors 'self'", wert.decode("latin-1")).encode("latin-1")
            aus.append((name, wert))
        return aus

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        for roh_name, roh_wert in scope["headers"]:
            if roh_name.lower() == b"origin" and not self._origin_erlaubt(roh_wert.decode("latin-1")):
                body = b"Nicht erlaubt."
                await send({"type": "http.response.start", "status": 403, "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ]})
                await send({"type": "http.response.body", "body": body})
                return
        if pfad_unzulaessig(scope):
            body = "Ungültiger Pfad".encode("utf-8")
            await send({"type": "http.response.start", "status": 400, "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        if self._sperre.gesperrt() and ist_ki_pfad(scope["path"]):
            body = json.dumps({"error": SPERR_TEXT}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 423, "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ]})
            await send({"type": "http.response.body", "body": body})
            return

        async def koerper() -> AsyncIterator[bytes]:
            while True:
                nachricht = await receive()
                if nachricht["type"] == "http.disconnect":
                    return
                if nachricht.get("body"):
                    yield nachricht["body"]
                if not nachricht.get("more_body"):
                    return

        url = self._basis + _pfad_und_query(scope)
        gestartet = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0), follow_redirects=False) as client:
                anfrage = client.build_request(
                    scope["method"], url, headers=self._anfrage_kopf(scope),
                    content=koerper() if scope["method"] not in ("GET", "HEAD") else None,
                )
                antwort = await client.send(anfrage, stream=True)
                try:
                    await send({"type": "http.response.start", "status": antwort.status_code,
                                "headers": self._antwort_kopf(antwort.headers.raw)})
                    gestartet = True
                    async for stueck in antwort.aiter_raw():
                        await send({"type": "http.response.body", "body": stueck, "more_body": True})
                    await send({"type": "http.response.body", "body": b""})
                finally:
                    await antwort.aclose()
        except (httpx.HTTPError, OSError):
            if gestartet:
                return  # Antwort lief schon; der Abbruch zeigt sich dem Browser als Abbruch
            body = NICHT_ERREICHBAR.encode("utf-8")
            await send({"type": "http.response.start", "status": 502, "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ]})
            await send({"type": "http.response.body", "body": body})

    # --- Websocket ----------------------------------------------------------------------

    async def _ws(self, scope: Scope, receive: Receive, send: Send) -> None:
        nachricht = await receive()
        if nachricht["type"] != "websocket.connect":
            return
        origin = next((w.decode("latin-1") for n, w in scope["headers"] if n.lower() == b"origin"), None)
        if origin is None or not self._origin_erlaubt(origin):
            await send({"type": "websocket.close", "code": 1008})
            return
        ki_pfad = ist_ki_pfad(scope["path"])
        if pfad_unzulaessig(scope) or (ki_pfad and self._sperre.gesperrt()):
            await send({"type": "websocket.close", "code": 1008, "reason": SPERR_TEXT})
            return
        kopf = [(n.decode("latin-1"), w.decode("latin-1")) for n, w in scope["headers"]
                if n.decode("latin-1").lower() not in _WS_KOPF | _HOP]
        try:
            upstream = await websockets.connect(
                self._ws_basis + _pfad_und_query(scope), origin=self._basis, extra_headers=kopf,
                subprotocols=list(scope.get("subprotocols") or []) or None,
                open_timeout=10, max_size=None,
            )
        except Exception:  # Verbindungs-, Handshake- oder Timeout-Fehler: einheitlich "weg"
            await send({"type": "websocket.close", "code": 1011})
            return
        await send({"type": "websocket.accept", "subprotocol": upstream.subprotocol})

        closed = asyncio.Event()

        async def hoch() -> None:
            while True:
                n = await receive()
                if n["type"] == "websocket.disconnect":
                    return
                if n["type"] == "websocket.receive":
                    if ki_pfad and self._sperre.gesperrt():  # Sperre kann nach dem Verbindungsaufbau greifen
                        await send({"type": "websocket.close", "code": 1008, "reason": SPERR_TEXT})
                        closed.set()
                        return
                    if n.get("text") is not None:
                        await upstream.send(n["text"])
                    elif n.get("bytes") is not None:
                        await upstream.send(n["bytes"])

        async def runter() -> None:
            async for m in upstream:
                if isinstance(m, str):
                    await send({"type": "websocket.send", "text": m})
                else:
                    await send({"type": "websocket.send", "bytes": bytes(m)})

        aufgaben = [asyncio.ensure_future(hoch()), asyncio.ensure_future(runter())]
        try:
            await asyncio.wait(aufgaben, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for a in aufgaben:
                a.cancel()
            await asyncio.gather(*aufgaben, return_exceptions=True)
            await upstream.close()
            try:
                if not closed.is_set():
                    await send({"type": "websocket.close", "code": 1000})
            except Exception:  # Gegenseite schon zu
                pass
