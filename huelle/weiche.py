"""ASGI-Weichen der beiden Ports (Ruling J): Cookie- und Host-Pruefung je Port, dann Verteilung.

Seiten-Port (`LEARNING_HUELLE_BASIS`): nur `/lernen/*` (Zugang, Seite, API); schreibende Anfragen
nur mit eigener `Origin`. Flaechen-Port (`LEARNING_HUELLE_FLAECHE_BASIS`): nur PenEcho; `/lernen/*` gibt
es dort nicht. PenEcho-Widgets laufen mit der Flaechen-Herkunft und erreichen die Seite so nicht.

Wer fragt (Teil B): die Weiche legt je Anfrage `scope["state"]["actor_id"]` fest; das ist der eine Weg,
auf dem die Kennung zur Bruecke kommt (`request.state.actor_id`). Ein gueltiges Personen-Cookie
(`huelle_person`, gesetzt von `/lernen/uebergabe/<token>`) gewinnt; sonst gilt das Zugangscookie des
Notwegs (`huelle_zugang`) mit der Inhaber-Kennung aus der Umgebung. Ohne `LEARNING_HUELLE_LEARNHOUSE_URL`
gibt es weder die Uebergabe noch Personen-Cookies.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from http.cookies import CookieError, SimpleCookie
from typing import Any

from .einstellung import Einstellung
from .person import PERSON_COOKIE, PERSON_GUELTIG, person_aus_cookie, person_cookie
from .uebergabe import uebergabe_lesen
from .zugang import COOKIE, GUELTIG_SEKUNDEN, token_erzeugen, token_gueltig

ZUGANG_PREFIX = "/lernen/zugang/"
UEBERGABE_PREFIX = "/lernen/uebergabe/"
ABGELAUFEN = "Dieser Link ist abgelaufen oder ungültig."
NICHT_ERLAUBT = "Nicht erlaubt."
WEITER = "Weiter zum Lernen"
_LESEND = frozenset({"GET", "HEAD"})
# PenEcho liefert diese Vendor-Skripte selbst oeffentlich aus (`Access-Control-Allow-Origin: *`,
# `Cross-Origin-Resource-Policy: cross-origin`; PenEcho src/server/main.js ca. Zeilen 4163-4180).
# Widgets laufen in Sandbox-Frames mit opakem Origin und laden sie ohne Cookie; deshalb gehen genau
# diese Pfade (GET/HEAD, exakter Pfad, Query erlaubt) auf dem Flaechen-Port ohne Cookie durch.
OEFFENTLICHE_PENECHO_DATEIEN = frozenset({
    "/widget-renderer.js",
    "/ink-lab-vendor.js",
    "/playground/liveclay-v1.js",
    "/visual-explainer-vendor.js",
})
_OEFFENTLICH_ROH = frozenset(p.encode() for p in OEFFENTLICHE_PENECHO_DATEIEN)
# Die Zugangsseite setzt das Cookie mit 200 und leitet erst dann weiter: so ist die Navigation zu
# /lernen/ eine eigene (same-site), und das SameSite=Strict-Cookie geht mit, auch wenn der Link aus
# einer anderen App (Telegram) kam. Kein Skript, daher passt die strenge CSP.
def _weiter_html(ziel: str) -> str:
    ziel = html.escape(ziel, quote=True)
    return (
        "<!doctype html>\n<html lang=\"de\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<meta http-equiv=\"refresh\" content=\"0;url={ziel}\">\n<title>Lernen</title>\n</head>\n"
        f"<body>\n<p><a href=\"{ziel}\">{WEITER}</a></p>\n</body>\n</html>\n"
    )


_ZUGANG_HTML = _weiter_html("/lernen/")
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
    """Gemeinsame Pruefung: erlaubter Host des Ports und gueltiges Cookie; legt die Kennung der Anfrage fest."""

    def __init__(self, einstellung: Einstellung, hosts: frozenset[str]) -> None:
        self._e = einstellung
        self._hosts = hosts

    def _person(self, headers: dict[bytes, bytes]) -> str | None:
        """Kennung aus den Cookies: Personen-Cookie vor Zugangscookie (Notweg); sonst None."""
        roh = headers.get(b"cookie", b"").decode("latin-1")
        try:
            jar: SimpleCookie = SimpleCookie()
            jar.load(roh)
        except CookieError:
            return None
        person = jar.get(PERSON_COOKIE)
        if person is not None and self._e.learnhouse_url:
            actor = person_aus_cookie(self._e.dienst_schluessel, person.value)
            if actor is not None:
                return actor
        eintrag = jar.get(COOKIE)
        if eintrag is not None and token_gueltig(self._e.dienst_schluessel, eintrag.value):
            return self._e.actor_id
        return None

    def _erlaubt(self, scope: Scope) -> bool:
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1").lower()
        if host not in self._hosts:
            return False
        actor = self._person(headers)
        if actor is None:
            return False
        # Der eine Weg der Kennung zur Bruecke (request.state.actor_id); ueberschreibt jeden Vorwert.
        scope["state"] = {**(scope.get("state") or {}), "actor_id": actor}
        return True

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
        await self._cookie_setzen(send, cookie, _ZUGANG_HTML)

    async def _uebergabe(self, send: Send, token: str) -> None:
        """Uebergabe aus LearnHouse: Personen-Cookie (12 h) setzen, dann auf den Kurs weiterleiten."""
        try:
            actor_id, course_id = uebergabe_lesen(self._e.dienst_schluessel, token)
        except ValueError:
            await _text(send, 403, ABGELAUFEN)
            return
        wert = person_cookie(self._e.dienst_schluessel, actor_id)
        cookie = f"{PERSON_COOKIE}={wert}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={PERSON_GUELTIG}"
        # course_id ist eine kanonische UUID (vom Leser geprueft) und damit URL-sicher.
        ziel = f"/lernen/?kurs={course_id}" if course_id else "/lernen/"
        await self._cookie_setzen(send, cookie, _weiter_html(ziel))

    @staticmethod
    async def _cookie_setzen(send: Send, cookie: str, seite: str) -> None:
        body = seite.encode("utf-8")
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
        if (art == "http" and scope.get("method") == "GET" and path.startswith(UEBERGABE_PREFIX)
                and self._e.learnhouse_url):
            await self._uebergabe(send, path[len(UEBERGABE_PREFIX):])
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

    def _host_erlaubt(self, scope: Scope) -> bool:
        for name, wert in scope.get("headers", []):
            if name.lower() == b"host":
                return wert.decode("latin-1").lower() in self._hosts
        return False

    def _oeffentlich(self, scope: Scope) -> bool:
        # Rohe Bytes vergleichen: der Durchreicher leitet den ROHEN Pfad weiter; ein dekodierter
        # Vergleich (%2E, %00 ...) waere eine Parser-Differenz. Query gehoert nicht zum Pfad.
        roh = (scope.get("raw_path") or scope.get("path", "").encode()).split(b"?", 1)[0]
        return (scope["type"] == "http" and scope.get("method") in _LESEND
                and roh in _OEFFENTLICH_ROH and self._host_erlaubt(scope))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        art = scope["type"]
        if art == "lifespan":
            await _lifespan(receive, send)
            return
        if not self._oeffentlich(scope) and not self._erlaubt(scope):
            await self._abweisen(scope, send)
            return
        if _ist_lernen(scope.get("path", "")):
            if art == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await _text(send, 404, "Nicht gefunden")
            return
        await self._durchreicher(scope, receive, send)
