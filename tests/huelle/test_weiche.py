"""Weiche: Zugang, Cookie, Host und Verteilung der Pfade."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.weiche import Weiche
from spaces.learning.huelle.zugang import COOKIE, token_erzeugen

SCHLUESSEL = "k" * 40
HOST = "lernen.example.test"
E = einstellung_lesen({
    "LEARNING_HUELLE_BASIS": f"https://{HOST}",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
    "LEARNING_HUELLE_ACTOR": "lh-1-test",
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": "11111111-1111-4111-8111-111111111111", "titel": "T"}]),
})


class Aufzeichner:
    def __init__(self, name: str, aufrufe: list[str]) -> None:
        self.name, self.aufrufe = name, aufrufe

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.aufrufe.append(self.name)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _weiche() -> tuple[Weiche, list[str]]:
    aufrufe: list[str] = []
    return Weiche(E, Aufzeichner("seite", aufrufe), Aufzeichner("api", aufrufe), Aufzeichner("durch", aufrufe)), aufrufe


def _anfrage(weiche: Weiche, path: str, *, art: str = "http", host: str = HOST, cookie: str | None = None):
    headers = [(b"host", host.encode())]
    if cookie is not None:
        headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
    scope = {"type": art, "method": "GET", "path": path, "headers": headers}
    gesendet: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(nachricht: dict[str, Any]) -> None:
        gesendet.append(nachricht)

    asyncio.run(weiche(scope, receive, send))
    return gesendet


def _start(gesendet: list[dict[str, Any]]) -> dict[str, Any]:
    return gesendet[0]


def _gut() -> str:
    return token_erzeugen(SCHLUESSEL)


def test_zugang_setzt_cookie_und_leitet_um() -> None:
    weiche, aufrufe = _weiche()
    antwort = _start(_anfrage(weiche, f"/lernen/zugang/{_gut()}"))
    assert antwort["status"] == 303
    kopf = dict(antwort["headers"])
    assert kopf[b"location"] == b"/lernen/"
    cookie = kopf[b"set-cookie"].decode()
    assert cookie.startswith(f"{COOKIE}=")
    for teil in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=2592000"):
        assert teil in cookie
    assert aufrufe == []


def test_zugang_mit_ungueltigem_token_403() -> None:
    weiche, _ = _weiche()
    gesendet = _anfrage(weiche, "/lernen/zugang/muell")
    assert _start(gesendet)["status"] == 403
    assert gesendet[1]["body"].decode() == "Dieser Link ist abgelaufen oder ungültig."


def test_ohne_cookie_403_auch_fuer_penecho_pfade() -> None:
    weiche, aufrufe = _weiche()
    for path in ("/", "/api/ai/command", "/lernen/", "/lernen/api/x"):
        assert _start(_anfrage(weiche, path))["status"] == 403
    assert _start(_anfrage(weiche, "/", cookie="muell"))["status"] == 403
    assert aufrufe == []


def test_fremder_host_403() -> None:
    weiche, aufrufe = _weiche()
    assert _start(_anfrage(weiche, "/", host="evil.example", cookie=_gut()))["status"] == 403
    assert aufrufe == []


def test_weiche_verteilt_pfade() -> None:
    weiche, aufrufe = _weiche()
    cookie = _gut()
    for host, path in ((HOST, "/lernen/api/x"), (HOST, "/lernen/"), ("127.0.0.1:8150", "/app.js"), (HOST, "/lernen")):
        assert _start(_anfrage(weiche, path, host=host, cookie=cookie))["status"] == 200
    assert aufrufe == ["api", "seite", "durch", "seite"]


def test_websocket_ohne_cookie_1008() -> None:
    weiche, aufrufe = _weiche()
    assert _anfrage(weiche, "/ws", art="websocket") == [{"type": "websocket.close", "code": 1008}]
    assert _anfrage(weiche, "/ws", art="websocket", host="evil.example", cookie=_gut()) == [
        {"type": "websocket.close", "code": 1008}
    ]
    assert aufrufe == []
