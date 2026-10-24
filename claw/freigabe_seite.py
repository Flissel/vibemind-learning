"""Local approval page: the person publishes here, outside the model.

``GET /freigabe/<secret>`` shows what would be published; ``POST`` to the same address
publishes once. Requests must come from loopback with a loopback Host header (no DNS
rebinding), and a POST must carry this page's own Origin (no cross-site form posts).
"""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

from . import werkzeuge
from .freigabe import FreigabeAbgelehnt

PREFIX = "/freigabe/"
_SECRET = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}
_HEADERS = [
    (b"content-type", b"text/html; charset=utf-8"),
    (b"cache-control", b"no-store"),
    # Not "no-referrer": with it Chrome sends "Origin: null" on the form post and the
    # real click was refused (24.10.). "same-origin" still keeps the link from others.
    (b"referrer-policy", b"same-origin"),
    (b"x-content-type-options", b"nosniff"),
    (
        b"content-security-policy",
        b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    ),
]

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]


class FreigabeSeite:
    def __init__(self, port: int) -> None:
        self._hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        self._origins = {f"http://127.0.0.1:{port}".encode(), f"http://localhost:{port}".encode()}

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        headers = dict((k.lower(), v) for k, v in scope.get("headers", []))
        client = (scope.get("client") or ("", 0))[0]
        if client not in _LOOPBACK_CLIENTS or headers.get(b"host") not in self._hosts:
            await _antwort(send, 403, "Nicht erlaubt", "Diese Seite ist nur lokal erreichbar.")
            return
        geheimnis = scope["path"][len(PREFIX):]
        if not _SECRET.match(geheimnis):
            await _antwort(send, 404, "Nicht gefunden", "Diese Freigabe gibt es nicht.")
            return
        if scope["method"] == "GET":
            await self._zeigen(send, geheimnis)
        elif scope["method"] == "POST":
            if headers.get(b"origin") not in self._origins:
                await _antwort(send, 403, "Nicht erlaubt", "Freigabe nur von dieser Seite aus.")
                return
            await _koerper(receive)
            await self._veroeffentlichen(send, geheimnis)
        else:
            await _antwort(send, 405, "Nicht erlaubt", "Nur Anzeigen und Freigeben.")

    async def _zeigen(self, send: Send, geheimnis: str) -> None:
        try:
            anzeige = werkzeuge._freigaben.holen(geheimnis)
        except FreigabeAbgelehnt as error:
            await _antwort(send, 410, "Freigabe nicht mehr gueltig", error.grund)
            return
        await _antwort(send, 200, "Kurs veroeffentlichen?", _inhalt(anzeige.inhalt), knopf=True)

    async def _veroeffentlichen(self, send: Send, geheimnis: str) -> None:
        ergebnis = werkzeuge.freigeben(geheimnis)
        if ergebnis.get("ok"):
            daten = ergebnis["daten"]
            await _antwort(
                send, 200, "Veroeffentlicht",
                f"Der Auftrag {html.escape(str(daten.get('job_id')))} steht jetzt auf "
                f"{html.escape(str(daten.get('zustand')))}.",
            )
        else:
            await _antwort(
                send, 409, "Nicht veroeffentlicht", html.escape(str(ergebnis.get("fehler")))
            )


def _inhalt(inhalt: dict[str, Any]) -> str:
    teile = [f"<h2>{html.escape(str(inhalt.get('titel') or 'Ohne Titel'))}</h2>"]
    kapitel = inhalt.get("kapitel")
    if isinstance(kapitel, list):
        teile.append("<ol>")
        for k in kapitel:
            lektionen = "".join(f"<li>{html.escape(str(t))}</li>" for t in k.get("lektionen", []))
            teile.append(
                f"<li><strong>{html.escape(str(k.get('titel')))}</strong> "
                f"({int(k.get('aufgaben') or 0)} Aufgaben)<ul>{lektionen}</ul></li>"
            )
        teile.append("</ol>")
    else:
        teile.append("<p>Kapitel und Lektionen sind unbekannt.</p>")
    unbelegt = inhalt.get("unbelegte_aussagen") or []
    teile.append(
        f"<p>Unbelegte Aussagen: {len(unbelegt)}</p>"
        f"<p>Auftrag {html.escape(str(inhalt.get('job_id')))}, Revision "
        f"{int(inhalt.get('revision') or 0)}, Kursrevision {int(inhalt.get('kurs_revision') or 0)}</p>"
    )
    return "".join(teile)


async def _koerper(receive: Receive) -> dict[str, list[str]]:
    data = b""
    while True:
        message = await receive()
        data += message.get("body", b"")
        if not message.get("more_body") or len(data) > 4096:
            break
    return parse_qs(data.decode("utf-8", "replace"))


async def _antwort(send: Send, status: int, titel: str, text: str, *, knopf: bool = False) -> None:
    form = (
        '<form method="post"><button type="submit">Jetzt veroeffentlichen</button></form>'
        if knopf
        else ""
    )
    seite = (
        "<!doctype html><html lang=\"de\"><meta charset=\"utf-8\"><title>"
        f"{html.escape(titel)}</title><body style=\"font-family:sans-serif;max-width:40rem;"
        f"margin:2rem auto\"><h1>{html.escape(titel)}</h1>{text}{form}</body></html>"
    ).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [*_HEADERS, (b"content-length", str(len(seite)).encode())]})
    await send({"type": "http.response.body", "body": seite})
