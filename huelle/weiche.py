"""ASGI-Weiche: Zugang per Link, Cookie- und Host-Pruefung, dann Verteilung der Pfade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http.cookies import CookieError, SimpleCookie
from typing import Any

from .einstellung import Einstellung
from .zugang import COOKIE, GUELTIG_SEKUNDEN, token_erzeugen, token_gueltig

ZUGANG_PREFIX = "/lernen/zugang/"
ABGELAUFEN = "Dieser Link ist abgelaufen oder ungültig."

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


async def _text(send: Send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send({
        "type": "http.response.start", "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class Weiche:
    def __init__(self, einstellung: Einstellung, seite_app: App, api_app: App, durchreicher_app: App) -> None:
        self._e = einstellung
        self._seite = seite_app
        self._api = api_app
        self._durchreicher = durchreicher_app

    def _cookie_gueltig(self, headers: dict[bytes, bytes]) -> bool:
        roh = headers.get(b"cookie", b"").decode("latin-1")
        try:
            jar: SimpleCookie = SimpleCookie()
            jar.load(roh)
        except CookieError:
            return False
        eintrag = jar.get(COOKIE)
        return eintrag is not None and token_gueltig(self._e.dienst_schluessel, eintrag.value)

    def _erlaubt(self, scope: Scope) -> bool:
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1").lower()
        return host in self._e.erlaubte_hosts and self._cookie_gueltig(headers)

    async def _zugang(self, send: Send, token: str) -> None:
        if not token_gueltig(self._e.dienst_schluessel, token):
            await _text(send, 403, ABGELAUFEN)
            return
        wert = token_erzeugen(self._e.dienst_schluessel)
        cookie = f"{COOKIE}={wert}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={GUELTIG_SEKUNDEN}"
        await send({
            "type": "http.response.start", "status": 303,
            "headers": [
                (b"location", b"/lernen/"),
                (b"set-cookie", cookie.encode("ascii")),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        art = scope["type"]
        if art == "lifespan":
            while True:
                nachricht = await receive()
                if nachricht["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif nachricht["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        path = scope.get("path", "")
        if art == "http" and scope.get("method") == "GET" and path.startswith(ZUGANG_PREFIX):
            await self._zugang(send, path[len(ZUGANG_PREFIX):])
            return
        if not self._erlaubt(scope):
            if art == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await _text(send, 403, "Nicht erlaubt")
            return
        if path == "/lernen/api" or path.startswith("/lernen/api/"):
            await self._api(scope, receive, send)
        elif path == "/lernen" or path.startswith("/lernen/"):
            await self._seite(scope, receive, send)
        else:
            await self._durchreicher(scope, receive, send)
