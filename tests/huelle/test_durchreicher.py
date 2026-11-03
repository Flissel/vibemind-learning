"""Durchreicher gegen ein echtes lokales Fake-PenEcho (uvicorn im Thread)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
import websockets

from spaces.learning.huelle.durchreicher import SPERR_TEXT, Durchreicher
from spaces.learning.huelle.sperre import Sperre

CSP = "default-src 'self'; frame-ancestors 'none'; img-src data:"


class FakePenEcho:
    def __init__(self) -> None:
        self.aufrufe: list[str] = []
        self.kopf: dict[str, str] = {}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                m = await receive()
                if m["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] == "websocket":
            await receive()
            self.kopf = {k.decode(): v.decode() for k, v in scope["headers"]}
            await send({"type": "websocket.accept"})
            while True:
                m = await receive()
                if m["type"] == "websocket.disconnect":
                    return
                await send({"type": "websocket.send", "text": "echo:" + (m.get("text") or "")})
        pfad = scope["path"]
        self.aufrufe.append(pfad)
        self.kopf = {k.decode(): v.decode() for k, v in scope["headers"]}
        if pfad == "/sse":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
            await asyncio.sleep(1.0)
            await send({"type": "http.response.body", "body": b"data: 2\n\n"})
            return
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/plain"),
            (b"content-security-policy", CSP.encode()),
            (b"set-cookie", b"penecho_ai_session_x=abc; HttpOnly; SameSite=Strict"),
            (b"x-eigen", b"unveraendert"),
        ]})
        await send({"type": "http.response.body", "body": b"hallo"})


@pytest.fixture(scope="module")
def fake() -> Iterator[tuple[FakePenEcho, str]]:
    app = FakePenEcho()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="websockets"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


def _frei() -> Sperre:
    s = Sperre()
    s.frei()
    return s


async def _lauf(app: Any, pfad: str, *, methode: str = "GET", headers: list[tuple[bytes, bytes]] | None = None,
                body: bytes = b"") -> tuple[int, list[tuple[bytes, bytes]], list[tuple[float, bytes]]]:
    scope = {"type": "http", "method": methode, "path": pfad, "query_string": b"", "headers": headers or []}
    gesendet: list[dict[str, Any]] = []
    zeiten: list[tuple[float, bytes]] = []
    gegeben = False

    async def receive() -> dict[str, Any]:
        nonlocal gegeben
        if gegeben:
            await asyncio.sleep(3600)
        gegeben = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(m: dict[str, Any]) -> None:
        gesendet.append(m)
        if m["type"] == "http.response.body" and m.get("body"):
            zeiten.append((time.monotonic(), m["body"]))

    await app(scope, receive, send)
    start = gesendet[0]
    return start["status"], start["headers"], zeiten


def test_ki_pfad_gesperrt_423_ohne_upstream(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    app = Durchreicher(url, Sperre())  # Startzustand unbekannt
    status, headers, teile = asyncio.run(_lauf(app, "/api/ai/command", methode="POST", body=b"{}"))
    assert status == 423
    assert json.loads(b"".join(t for _, t in teile).decode("utf-8")) == {"error": "Während der Aufgabe hilft „Erklär mir das“."}
    assert SPERR_TEXT == "Während der Aufgabe hilft „Erklär mir das“."
    assert upstream.aufrufe == []


@pytest.mark.parametrize("pfad", ["/api/AI/command", "/api/ai/command/x", "/x/../api/suggest", "/api/%61i/command",
                                  "/api/plugins/improve"])
def test_ki_pfad_umgehungen_gesperrt(fake: tuple[FakePenEcho, str], pfad: str) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, _ = asyncio.run(_lauf(Durchreicher(url, Sperre()), pfad))
    assert status == 423
    assert upstream.aufrufe == []


def test_ki_pfad_frei_wird_durchgereicht(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, teile = asyncio.run(_lauf(Durchreicher(url, _frei()), "/api/ai/command", methode="POST", body=b"{}"))
    assert status == 200
    assert upstream.aufrufe == ["/api/ai/command"]
    assert b"".join(t for _, t in teile) == b"hallo"


def test_andere_pfade_trotz_sperre_durchgereicht(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, _ = asyncio.run(_lauf(Durchreicher(url, Sperre()), "/api/cloud/status"))
    assert status == 200
    assert upstream.aufrufe == ["/api/cloud/status"]


def test_csp_frame_ancestors_wird_self(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    _, headers, _ = asyncio.run(_lauf(Durchreicher(url, _frei()), "/"))
    csp = [v for k, v in headers if k == b"content-security-policy"]
    assert csp == [b"default-src 'self'; frame-ancestors 'self'; img-src data:"]
    assert (b"x-eigen", b"unveraendert") in headers


def test_host_und_origin_umgeschrieben(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    host = url.removeprefix("http://")
    asyncio.run(_lauf(Durchreicher(url, _frei()), "/", methode="POST", body=b"x", headers=[
        (b"host", b"lernen.example.test"), (b"origin", b"https://lernen.example.test"),
        (b"connection", b"keep-alive"), (b"cookie", b"a=b"),
    ]))
    assert upstream.kopf["host"] == host
    assert upstream.kopf["origin"] == url
    assert upstream.kopf["cookie"] == "a=b"
    assert upstream.kopf.get("connection") != "keep-alive, close"  # Hop-by-hop nicht durchgereicht


def test_origin_wird_nicht_erfunden(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    asyncio.run(_lauf(Durchreicher(url, _frei()), "/", headers=[(b"host", b"lernen.example.test")]))
    assert "origin" not in upstream.kopf


def test_set_cookie_bleibt(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    _, headers, _ = asyncio.run(_lauf(Durchreicher(url, _frei()), "/"))
    assert (b"set-cookie", b"penecho_ai_session_x=abc; HttpOnly; SameSite=Strict") in headers


def test_sse_wird_gestreamt(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    _, _, teile = asyncio.run(_lauf(Durchreicher(url, _frei()), "/sse"))
    daten = [(t, b) for t, b in teile if b]
    assert [b for _, b in daten] == [b"data: 1\n\n", b"data: 2\n\n"]
    assert daten[1][0] - daten[0][0] >= 0.7  # erstes Ereignis kam vor dem Ende an


def test_penecho_weg_502() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nichts lauscht mehr
    status, _, teile = asyncio.run(_lauf(Durchreicher(f"http://127.0.0.1:{port}", _frei()), "/"))
    assert status == 502
    assert b"".join(t for _, t in teile).decode("utf-8") == "Fläche weg"


async def _ws_lauf(app: Any, pfad: str, nachricht: str) -> list[dict[str, Any]]:
    scope = {"type": "websocket", "path": pfad, "query_string": b"", "headers": [
        (b"host", b"lernen.example.test"), (b"origin", b"https://lernen.example.test")]}
    gesendet: list[dict[str, Any]] = []
    eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    eingang.put_nowait({"type": "websocket.connect"})

    async def send(m: dict[str, Any]) -> None:
        gesendet.append(m)
        if m["type"] == "websocket.accept":
            eingang.put_nowait({"type": "websocket.receive", "text": nachricht})
        if m["type"] == "websocket.send":
            eingang.put_nowait({"type": "websocket.disconnect", "code": 1000})

    await asyncio.wait_for(app(scope, eingang.get, send), timeout=10)
    return gesendet


def test_websocket_durchgereicht(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    gesendet = asyncio.run(_ws_lauf(Durchreicher(url, Sperre()), "/api/mcp/canvas", "hi"))
    assert [m["type"] for m in gesendet][:2] == ["websocket.accept", "websocket.send"]
    assert gesendet[1]["text"] == "echo:hi"
    assert upstream.kopf["origin"] == url


def test_websocket_ki_pfad_gesperrt(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gesendet = asyncio.run(_ws_lauf(Durchreicher(url, Sperre()), "/api/canvas-agent/socket", "hi"))
    assert [m["type"] for m in gesendet] == ["websocket.close"]


def test_websocket_penecho_weg_schliesst() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    gesendet = asyncio.run(_ws_lauf(Durchreicher(f"http://127.0.0.1:{port}", _frei()), "/api/mcp/canvas", "hi"))
    assert [m["type"] for m in gesendet] == ["websocket.close"]
