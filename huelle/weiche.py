"""ASGI-Weichen der beiden Ports (Ruling J): Cookie- und Host-Pruefung je Port, dann Verteilung.

Seiten-Port (`LEARNING_HUELLE_BASIS`): nur `/lernen/*` (Zugang, Seite, API); schreibende Anfragen
nur mit eigener `Origin`. Flaechen-Port (`LEARNING_HUELLE_FLAECHE_BASIS`): nur PenEcho; `/lernen/*` gibt
es dort nicht. PenEcho-Widgets laufen mit der Flaechen-Herkunft und erreichen die Seite so nicht.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http.cookies import CookieError, SimpleCookie
from typing import Any

from .einstellung import Einstellung
from .zugang import COOKIE, GUELTIG_SEKUNDEN, token_erzeugen, token_gueltig

ZUGANG_PREFIX = "/lernen/zugang/"
ABGELAUFEN = "Dieser Link ist abgelaufen oder ungültig."
NICHT_ERLAUBT = "Nicht erlaubt."
WEITER = "Weiter zum Lernen"
_LESEND = frozenset({"GET", "HEAD"})
# Die Zugangsseite setzt das Cookie mit 200 und leitet erst dann weiter: so ist die Navigation zu
# /lernen/ eine eigene (same-site), und das SameSite=Strict-Cookie geht mit, auch wenn der Link aus
# einer anderen App (Telegram) kam. Kein Skript, daher passt die strenge CSP.
_ZUGANG_HTML = (
    "<!doctype html>\n<html lang=\"de\">\n<head>\n<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<meta http-equiv=\"refresh\" content=\"0;url=/lernen/\">\n<title>Lernen</title>\n</head>\n"
    f"<body>\n<p><a href=\"/lernen/\">{WEITER}</a></p>\n</body>\n</html>\n"
)
_ZUGANG_CSP = b"default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

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


async def _lifespan(receive: Receive, send: Send) -> None:
    while True:
        nachricht = await receive()
        if nachricht["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif nachricht["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def _ist_lernen(path: str) -> bool:
    return path == "/lernen" or path.startswith("/lernen/")


class _Tor:
    """Gemeinsame Pruefung: erlaubter Host des Ports und gueltiges Zugangscookie."""

    def __init__(self, einstellung: Einstellung, hosts: frozenset[str]) -> None:
        self._e = einstellung
        self._hosts = hosts

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
        return host in self._hosts and self._cookie_gueltig(headers)

    @staticmethod
    async def _abweisen(scope: Scope, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await _text(send, 403, "Nicht erlaubt")


class Weiche(_Tor):
    """Seiten-Port: Zugang, Seite und API unter `/lernen/`; alles andere 404."""

    def __init__(self, einstellung: Einstellung, seite_app: App, api_app: App) -> None:
        super().__init__(einstellung, einstellung.erlaubte_hosts)
        self._seite = seite_app
        self._api = api_app
        self._herkuenfte = frozenset(h.lower() for h in einstellung.seiten_herkuenfte)

    def _origin_eigen(self, scope: Scope) -> bool:
        origin = next((w for n, w in scope.get("headers", []) if n.lower() == b"origin"), None)
        return origin is not None and origin.decode("latin-1").lower() in self._herkuenfte

    async def _zugang(self, send: Send, token: str) -> None:
        if not token_gueltig(self._e.dienst_schluessel, token):
            await _text(send, 403, ABGELAUFEN)
            return
        wert = token_erzeugen(self._e.dienst_schluessel)
        cookie = f"{COOKIE}={wert}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={GUELTIG_SEKUNDEN}"
        body = _ZUGANG_HTML.encode("utf-8")
        await send({
            "type": "http.response.start", "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"set-cookie", cookie.encode("ascii")),
                (b"content-security-policy", _ZUGANG_CSP),
                (b"referrer-policy", b"no-referrer"),
                (b"x-content-type-options", b"nosniff"),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        art = scope["type"]
        if art == "lifespan":
            await _lifespan(receive, send)
            return
        path = scope.get("path", "")
        if art == "http" and scope.get("method") == "GET" and path.startswith(ZUGANG_PREFIX):
            await self._zugang(send, path[len(ZUGANG_PREFIX):])
            return
        if not self._erlaubt(scope):
            await self._abweisen(scope, send)
            return
        if art == "websocket":  # Seite und API haben keine Websockets
            await send({"type": "websocket.close", "code": 1008})
            return
        if art != "http":
            return
        if not _ist_lernen(path):
            await _text(send, 404, "Nicht gefunden")
            return
        # Schreibend nur von der eigenen Seite: schuetzt vor Anfragen anderer Ports desselben Hosts
        # (same-site, das Cookie geht mit) und vor PenEcho-Widgets.
        if scope.get("method") not in _LESEND and not self._origin_eigen(scope):
            await _text(send, 403, NICHT_ERLAUBT)
            return
        if path == "/lernen/api" or path.startswith("/lernen/api/"):
            await self._api(scope, receive, send)
        else:
            await self._seite(scope, receive, send)


class FlaechenWeiche(_Tor):
    """Flaechen-Port: alles an den Durchreicher (PenEcho), ausser `/lernen/*` (404)."""

    def __init__(self, einstellung: Einstellung, durchreicher_app: App) -> None:
        super().__init__(einstellung, einstellung.flaechen_hosts)
        self._durchreicher = durchreicher_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        art = scope["type"]
        if art == "lifespan":
            await _lifespan(receive, send)
            return
        if not self._erlaubt(scope):
            await self._abweisen(scope, send)
            return
        if _ist_lernen(scope.get("path", "")):
            if art == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await _text(send, 404, "Nicht gefunden")
            return
        await self._durchreicher(scope, receive, send)
