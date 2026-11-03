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

EIGEN = frozenset({"https://lernen.example.test", "http://127.0.0.1:8099"})
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
    scope = {"type": "http", "method": methode, "path": pfad, "raw_path": pfad.encode("utf-8"),
             "query_string": b"", "headers": headers or []}
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
    app = Durchreicher(url, Sperre(), EIGEN)  # Startzustand unbekannt
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
    status, _, _ = asyncio.run(_lauf(Durchreicher(url, Sperre(), EIGEN), pfad))
    assert status == 423
    assert upstream.aufrufe == []


@pytest.mark.parametrize("pfad", ["/api\\ai\\command", "/api\\ai/command", "/api/ai\tcommand", "/api/ai/comm\nand"])
def test_ki_pfad_backslash_und_steuerzeichen_nicht_durch(fake: tuple[FakePenEcho, str], pfad: str) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, _ = asyncio.run(_lauf(Durchreicher(url, Sperre(), EIGEN), pfad, methode="POST", body=b"{}"))
    assert status in (400, 423)
    assert upstream.aufrufe == []


def test_ki_pfad_frei_wird_durchgereicht(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, teile = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/api/ai/command", methode="POST", body=b"{}"))
    assert status == 200
    assert upstream.aufrufe == ["/api/ai/command"]
    assert b"".join(t for _, t in teile) == b"hallo"


def test_andere_pfade_trotz_sperre_durchgereicht(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, _ = asyncio.run(_lauf(Durchreicher(url, Sperre(), EIGEN), "/api/cloud/status"))
    assert status == 200
    assert upstream.aufrufe == ["/api/cloud/status"]


def test_csp_ohne_rahmen_herkunft_bleibt_none(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    _, headers, _ = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/"))
    csp = [v for k, v in headers if k == b"content-security-policy"]
    assert csp == [b"default-src 'self'; frame-ancestors 'none'; img-src data:"]
    assert (b"x-eigen", b"unveraendert") in headers


def test_host_und_origin_umgeschrieben(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    host = url.removeprefix("http://")
    asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/", methode="POST", body=b"x", headers=[
        (b"host", b"lernen.example.test"), (b"origin", b"https://lernen.example.test"),
        (b"connection", b"keep-alive"), (b"cookie", b"a=b"),
    ]))
    assert upstream.kopf["host"] == host
    assert upstream.kopf["origin"] == url
    assert upstream.kopf["cookie"] == "a=b"
    assert upstream.kopf.get("connection") != "keep-alive, close"  # Hop-by-hop nicht durchgereicht


def test_origin_wird_nicht_erfunden(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/", headers=[(b"host", b"lernen.example.test")]))
    assert "origin" not in upstream.kopf


def test_set_cookie_bleibt(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    _, headers, _ = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/"))
    assert (b"set-cookie", b"penecho_ai_session_x=abc; HttpOnly; SameSite=Strict") in headers


def test_sse_wird_gestreamt(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    _, _, teile = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/sse"))
    daten = [(t, b) for t, b in teile if b]
    assert [b for _, b in daten] == [b"data: 1\n\n", b"data: 2\n\n"]
    assert daten[1][0] - daten[0][0] >= 0.7  # erstes Ereignis kam vor dem Ende an


def test_penecho_weg_502() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nichts lauscht mehr
    status, _, teile = asyncio.run(_lauf(Durchreicher(f"http://127.0.0.1:{port}", _frei(), EIGEN), "/"))
    assert status == 502
    assert b"".join(t for _, t in teile).decode("utf-8") == "Die Fläche ist gerade nicht erreichbar."


async def _ws_lauf(app: Any, pfad: str, nachricht: str) -> list[dict[str, Any]]:
    scope = {"type": "websocket", "path": pfad, "raw_path": pfad.encode("utf-8"), "query_string": b"", "headers": [
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
    gesendet = asyncio.run(_ws_lauf(Durchreicher(url, Sperre(), EIGEN), "/api/mcp/canvas", "hi"))
    assert [m["type"] for m in gesendet][:2] == ["websocket.accept", "websocket.send"]
    assert gesendet[1]["text"] == "echo:hi"
    assert upstream.kopf["origin"] == url


def test_websocket_ki_pfad_gesperrt(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gesendet = asyncio.run(_ws_lauf(Durchreicher(url, Sperre(), EIGEN), "/api/canvas-agent/socket", "hi"))
    assert [m["type"] for m in gesendet] == ["websocket.close"]


def test_websocket_penecho_weg_schliesst() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    gesendet = asyncio.run(_ws_lauf(Durchreicher(f"http://127.0.0.1:{port}", _frei(), EIGEN), "/api/mcp/canvas", "hi"))
    assert [m["type"] for m in gesendet] == ["websocket.close"]


def test_websocket_backslash_pfad_gesperrt(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gesendet = asyncio.run(_ws_lauf(Durchreicher(url, Sperre(), EIGEN), "/api\\canvas-agent\\socket", "hi"))
    assert [m["type"] for m in gesendet] == ["websocket.close"]


def test_websocket_wird_bei_neuer_sperre_geschlossen(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    sperre = _frei()

    async def lauf() -> list[dict[str, Any]]:
        app = Durchreicher(url, sperre, EIGEN)
        scope = {"type": "websocket", "path": "/api/canvas-agent/socket", "raw_path": b"/api/canvas-agent/socket",
                 "query_string": b"",
                 "headers": [(b"origin", b"https://lernen.example.test")]}
        gesendet: list[dict[str, Any]] = []
        eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        eingang.put_nowait({"type": "websocket.connect"})

        async def send(m: dict[str, Any]) -> None:
            gesendet.append(m)
            if m["type"] == "websocket.accept":
                eingang.put_nowait({"type": "websocket.receive", "text": "eins"})
            elif m["type"] == "websocket.send" and m["text"] == "echo:eins":
                sperre.offen("naechste-aufgabe")
                eingang.put_nowait({"type": "websocket.receive", "text": "zwei"})

        await asyncio.wait_for(app(scope, eingang.get, send), timeout=10)
        return gesendet

    gesendet = asyncio.run(lauf())
    texte = [m.get("text") for m in gesendet if m["type"] == "websocket.send"]
    assert texte == ["echo:eins"]
    assert gesendet[-1]["type"] == "websocket.close" and gesendet[-1]["code"] == 1008


def test_http_fremde_herkunft_403_ohne_upstream(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    for fremd in (b"https://boese.example.test", b"null", b"HTTPS://lernen.example.test:444",
                  b"https://lernen.example.test/"):
        status, _, teile = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/api/local-access/pin",
                                             methode="POST", body=b"{}", headers=[(b"origin", fremd)]))
        assert status == 403, fremd
        assert b"".join(t for _, t in teile) == b"Nicht erlaubt."
    assert upstream.aufrufe == []


def test_http_eigene_herkunft_wird_umgeschrieben(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    for eigen in (b"https://lernen.example.test", b"HTTPS://Lernen.Example.test", b"http://127.0.0.1:8099"):
        asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/", methode="POST", body=b"{}",
                          headers=[(b"origin", eigen)]))
        assert upstream.kopf["origin"] == url


def _ws_mit_origin(app: Any, origin: bytes | None) -> list[dict[str, Any]]:
    async def lauf() -> list[dict[str, Any]]:
        scope = {"type": "websocket", "path": "/api/mcp/canvas", "raw_path": b"/api/mcp/canvas", "query_string": b"",
                 "headers": [] if origin is None else [(b"origin", origin)]}
        gesendet: list[dict[str, Any]] = []
        eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        eingang.put_nowait({"type": "websocket.connect"})

        async def send(m: dict[str, Any]) -> None:
            gesendet.append(m)
            if m["type"] == "websocket.accept":
                eingang.put_nowait({"type": "websocket.receive", "text": "hi"})
            if m["type"] == "websocket.send":
                eingang.put_nowait({"type": "websocket.disconnect", "code": 1000})

        await asyncio.wait_for(app(scope, eingang.get, send), timeout=10)
        return gesendet

    return asyncio.run(lauf())


def test_websocket_ohne_origin_1008(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gesendet = _ws_mit_origin(Durchreicher(url, _frei(), EIGEN), None)
    assert [(m["type"], m.get("code")) for m in gesendet] == [("websocket.close", 1008)]


def test_websocket_fremde_origin_1008(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    for fremd in (b"https://boese.example.test", b"null"):
        gesendet = _ws_mit_origin(Durchreicher(url, _frei(), EIGEN), fremd)
        assert [(m["type"], m.get("code")) for m in gesendet] == [("websocket.close", 1008)]


def test_websocket_eigene_origin_echo(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    gesendet = _ws_mit_origin(Durchreicher(url, _frei(), EIGEN), b"https://lernen.example.test")
    assert gesendet[1]["text"] == "echo:hi"
    assert upstream.kopf["origin"] == url


# ---- Abschlusspruefung: Cloud-Kopplung, Pfade, Zugangscookie, websockets-API -------------

@pytest.mark.parametrize("pfad", [
    "/api/cloud/sign-in", "/api/cloud/sign-in/start", "/api/cloud/sign-in/callback", "/api/cloud/pair",
    "/api/cloud/device/enable", "/api/cloud/mcp/access", "/api/CLOUD/Pair", "/x/../api/cloud/pair",
    "/api/cloud/%70air",
])
def test_cloud_kopplung_immer_403_auch_bei_freier_sperre(fake: tuple[FakePenEcho, str], pfad: str) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, teile = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), pfad, methode="POST", body=b"{}"))
    assert status == 403, pfad
    assert b"".join(t for _, t in teile) == "Nicht erlaubt.".encode()
    assert upstream.aufrufe == []


def test_cloud_kopplung_websocket_1008(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gesendet = asyncio.run(_ws_lauf(Durchreicher(url, _frei(), EIGEN), "/api/cloud/pair", "hi"))
    assert [(m["type"], m.get("code")) for m in gesendet] == [("websocket.close", 1008)]


def test_andere_cloud_pfade_bleiben_offen(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    for pfad in ("/api/cloud/status", "/api/cloud/sign-out", "/api/cloud/device/disable"):
        status, _, _ = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), pfad))
        assert status == 200, pfad
    assert upstream.aufrufe == ["/api/cloud/status", "/api/cloud/sign-out", "/api/cloud/device/disable"]


@pytest.mark.parametrize("roh", [b"api/ai/command", b"*", b"http://evil.example/x"])
def test_pfad_ohne_schraegstrich_400(fake: tuple[FakePenEcho, str], roh: bytes) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    scope_pfad = roh.decode()

    async def lauf() -> int:
        scope = {"type": "http", "method": "GET", "path": scope_pfad, "raw_path": roh, "query_string": b"",
                 "headers": []}
        gesendet: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m: dict[str, Any]) -> None:
            gesendet.append(m)

        await Durchreicher(url, _frei(), EIGEN)(scope, receive, send)
        return gesendet[0]["status"]

    assert asyncio.run(lauf()) == 400
    assert upstream.aufrufe == []


def test_ungueltige_url_wird_502(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake

    async def lauf() -> int:
        scope = {"type": "http", "method": "GET", "path": "/", "raw_path": b"/", "query_string": b"a=\x01",
                 "headers": []}
        gesendet: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m: dict[str, Any]) -> None:
            gesendet.append(m)

        await Durchreicher(url, _frei(), EIGEN)(scope, receive, send)
        return gesendet[0]["status"]

    assert asyncio.run(lauf()) == 502


def test_zugangscookie_geht_nicht_an_penecho(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/", headers=[
        (b"cookie", b"huelle_zugang=geheim; penecho_ai_session_x=abc"),
    ]))
    assert upstream.kopf["cookie"] == "penecho_ai_session_x=abc"
    asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/", headers=[(b"cookie", b"huelle_zugang=geheim")]))
    assert "cookie" not in upstream.kopf


def test_zugangscookie_geht_nicht_an_penecho_websocket(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake

    async def lauf() -> None:
        scope = {"type": "websocket", "path": "/api/mcp/canvas", "raw_path": b"/api/mcp/canvas", "query_string": b"",
                 "headers": [(b"origin", b"https://lernen.example.test"),
                             (b"cookie", b"huelle_zugang=geheim; a=b")]}
        eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        eingang.put_nowait({"type": "websocket.connect"})

        async def send(m: dict[str, Any]) -> None:
            if m["type"] == "websocket.accept":
                eingang.put_nowait({"type": "websocket.receive", "text": "hi"})
            if m["type"] == "websocket.send":
                eingang.put_nowait({"type": "websocket.disconnect", "code": 1000})

        await asyncio.wait_for(Durchreicher(url, _frei(), EIGEN)(scope, eingang.get, send), timeout=10)

    asyncio.run(lauf())
    assert "huelle_zugang" not in upstream.kopf.get("cookie", "")
    assert upstream.kopf.get("cookie") == "a=b"


def test_websockets_kopf_argument_je_version() -> None:
    from spaces.learning.huelle.durchreicher import _kopf_argument

    def neu(uri: str, *, additional_headers: Any = None, origin: Any = None) -> None: ...

    def alt(uri: str, *, extra_headers: Any = None, origin: Any = None) -> None: ...

    assert _kopf_argument(neu) == "additional_headers"
    assert _kopf_argument(alt) == "extra_headers"
    erwartet = "additional_headers" if int(websockets.__version__.split(".")[0]) >= 14 else "extra_headers"
    assert _kopf_argument(websockets.connect) == erwartet


def test_frame_ancestors_auf_seitenherkunft(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    seite = ("https://lernen.example.test", "http://127.0.0.1:8150")
    app = Durchreicher(url, _frei(), EIGEN, rahmen_herkuenfte=seite)
    _, headers, _ = asyncio.run(_lauf(app, "/"))
    csp = [v for k, v in headers if k == b"content-security-policy"]
    assert csp == [b"default-src 'self'; frame-ancestors https://lernen.example.test http://127.0.0.1:8150; "
                   b"img-src data:"]
