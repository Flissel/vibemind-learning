"""Durchreicher gegen ein echtes lokales Fake-PenEcho (uvicorn im Thread)."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
import websockets

from spaces.learning.huelle.durchreicher import SPERR_TEXT, Durchreicher
from spaces.learning.huelle.gruppe import (
    GRUPPE_WEG,
    KEIN_MITGLIED,
    ZUGANG_WEG,
    KeinMitglied,
    Mitgliedschaft,
    ausweis,
    person_kennung,
)
from spaces.learning.huelle.katalog import UebersichtWeg
from spaces.learning.huelle.sperre import Sperre

EIGEN = frozenset({"https://lernen.example.test", "http://127.0.0.1:8099"})
CSP = "default-src 'self'; frame-ancestors 'none'; img-src data:"


# Kopf von PenEchos index.html (src/server/main.js ca. Z. 4488, gekuerzt).
CSP_INDEX = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; connect-src 'self'; "
             "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
INDEX = (b"<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<title>PenEcho</title>\n"
         b"<script src=\"/api/config.js\"></script>\n</head>\n<body>\n" + b"<p>Fl\xc3\xa4che</p>\n" * 200
         + b"<script src=\"app.js\"></script>\n</body>\n</html>\n")
SKRIPT = b"console.log('app');\n" * 100
SEITEN = {
    "/": (b"text/html; charset=utf-8", INDEX),
    "/index.html": (b"text/html; charset=utf-8", INDEX),
    "/widget-host.html": (b"text/html; charset=utf-8", INDEX),
    "/app.js": (b"text/javascript; charset=utf-8", SKRIPT),
}


# Server-Ablage: ein Board von A, eins von B, eins ohne documentId (alt).
BOARD_A, BOARD_B, BOARD_ALT = "1700000000000-aaaaaaaa", "1700000000001-bbbbbbbb", "1700000000002-cccccccc"
BOARDS = [{"id": BOARD_A, "documentId": "doc-a", "name": "A"}, {"id": BOARD_B, "documentId": "doc-b", "name": "B"},
          {"id": BOARD_ALT, "documentId": None, "name": "alt"}]


class FakePenEcho:
    def __init__(self) -> None:
        self.aufrufe: list[str] = []
        self.abfragen: list[bytes] = []
        self.kopf: dict[str, str] = {}
        self.immer_gzip = False
        # PenEcho 1.4.x: nach jedem Neustart "undecided", bis jemand Code oder "offen" waehlt.
        self.zugang = "open"
        self.geoeffnet: list[tuple[str | None, str | None, bytes]] = []
        self.ablage: list[tuple[str, str, bytes, bytes]] = []  # (Methode, Pfad, Query, Body) an die Ablage
        self.ws_ziele: list[tuple[str, bytes]] = []  # (Pfad, Query) je Websocket

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
            self.ws_ziele.append((scope["path"], scope.get("query_string", b"")))
            await send({"type": "websocket.accept"})
            while True:
                m = await receive()
                if m["type"] == "websocket.disconnect":
                    return
                await send({"type": "websocket.send", "text": "echo:" + (m.get("text") or "")})
        pfad = scope["path"]
        self.aufrufe.append(pfad)
        self.abfragen.append(scope.get("query_string", b""))
        self.kopf = {k.decode(): v.decode() for k, v in scope["headers"]}
        if pfad in ("/api/local-access/status", "/api/local-access/open"):
            m = await receive()
            if pfad.endswith("/open"):
                self.geoeffnet.append((self.kopf.get("origin"), self.kopf.get("content-type"), m.get("body", b"")))
                self.zugang = "open"
            body = json.dumps({"mode": self.zugang, "setupRequired": self.zugang == "undecided"}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        if pfad in SEITEN:
            # Wie PenEchos static-response.js: gzip, wenn erlaubt (oder hier erzwungen).
            await receive()
            art, roh = SEITEN[pfad]
            gz = self.immer_gzip or "gzip" in self.kopf.get("accept-encoding", "")
            body = gzip.compress(roh) if gz else roh
            kopf = [(b"content-type", art), (b"content-security-policy", CSP_INDEX.encode()),
                    (b"etag", b'W/"abc"'), (b"vary", b"Accept-Encoding"),
                    (b"content-length", str(len(body)).encode())]
            if gz:
                kopf.append((b"content-encoding", b"gzip"))
            await send({"type": "http.response.start", "status": 200, "headers": kopf})
            await send({"type": "http.response.body", "body": body if scope["method"] != "HEAD" else b""})
            return
        if pfad.startswith("/api/canvas"):
            # PenEchos Server-Ablage (main.js ca. 3993-4045), stark vereinfacht.
            body_in = (await receive()).get("body", b"")
            self.ablage.append((scope["method"], pfad, scope.get("query_string", b""), body_in))
            if scope["method"] == "GET" and pfad == "/api/canvases":
                daten: dict[str, Any] = {"canvases": BOARDS}
            elif pfad.startswith("/api/canvases/"):
                daten = {"canvas": {"id": pfad.split("/")[3]}}
            else:
                daten = {"ok": True}
            body = json.dumps(daten).encode()
            await send({"type": "http.response.start", "status": 201 if scope["method"] == "POST" else 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
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
                body: bytes = b"", query: bytes = b"", state: dict[str, Any] | None = None,
                ) -> tuple[int, list[tuple[bytes, bytes]], list[tuple[float, bytes]]]:
    scope = {"type": "http", "method": methode, "path": pfad, "raw_path": pfad.encode("utf-8"),
             "query_string": query, "headers": headers or []}
    if state is not None:
        scope["state"] = state
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
    _, headers, _ = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/x"))
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
    _, headers, _ = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/x"))
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


def test_personencookie_geht_nicht_an_penecho(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/", headers=[
        (b"cookie", b"huelle_person=geheim; penecho_ai_session_x=abc; huelle_zugang=auch"),
    ]))
    assert upstream.kopf["cookie"] == "penecho_ai_session_x=abc"
    asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/", headers=[(b"cookie", b"huelle_person=geheim")]))
    assert "cookie" not in upstream.kopf


def test_personencookie_geht_nicht_an_penecho_websocket(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake

    async def lauf() -> None:
        scope = {"type": "websocket", "path": "/api/mcp/canvas", "raw_path": b"/api/mcp/canvas", "query_string": b"",
                 "headers": [(b"origin", b"https://lernen.example.test"),
                             (b"cookie", b"huelle_person=geheim; a=b")]}
        eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        eingang.put_nowait({"type": "websocket.connect"})

        async def send(m: dict[str, Any]) -> None:
            if m["type"] == "websocket.accept":
                eingang.put_nowait({"type": "websocket.receive", "text": "hi"})
            if m["type"] == "websocket.send":
                eingang.put_nowait({"type": "websocket.disconnect", "code": 1000})

        await asyncio.wait_for(Durchreicher(url, _frei(), EIGEN)(scope, eingang.get, send), timeout=10)

    asyncio.run(lauf())
    assert "huelle_person" not in upstream.kopf.get("cookie", "")
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
    _, headers, _ = asyncio.run(_lauf(app, "/x"))
    csp = [v for k, v in headers if k == b"content-security-policy"]
    assert csp == [b"default-src 'self'; frame-ancestors https://lernen.example.test http://127.0.0.1:8150; "
                   b"img-src data:"]


def _csp_nach(app: Durchreicher, csp: str) -> str:
    aus = app._antwort_kopf([(b"Content-Security-Policy", csp.encode("latin-1"))])
    return aus[0][1].decode("latin-1")


def test_frame_ancestors_self_bekommt_seitenherkuenfte() -> None:
    seite = ("https://lernen.example.test", "http://127.0.0.1:8150")
    app = Durchreicher("http://127.0.0.1:1", _frei(), EIGEN, rahmen_herkuenfte=seite)
    csp = _csp_nach(app, "default-src 'self'; frame-ancestors 'self' http://localhost:3888; img-src data:")
    assert csp == ("default-src 'self'; frame-ancestors 'self' http://localhost:3888 "
                   "https://lernen.example.test http://127.0.0.1:8150; img-src data:")


def test_frame_ancestors_self_ohne_seitenherkunft_unveraendert() -> None:
    app = Durchreicher("http://127.0.0.1:1", _frei(), EIGEN)
    assert _csp_nach(app, "frame-ancestors 'self'") == "frame-ancestors 'self'"


def test_frame_ancestors_self_keine_doppelten_herkuenfte() -> None:
    app = Durchreicher("http://127.0.0.1:1", _frei(), EIGEN, rahmen_herkuenfte=("http://a.test",))
    assert _csp_nach(app, "frame-ancestors 'self' http://a.test") == "frame-ancestors 'self' http://a.test"


# ---- Speichern ueber PenEchos eigenen Knopf: Skript in PenEchos Seite ----------------------

SEITE_HERKUNFT = ("https://lernen.example.test", "http://127.0.0.1:8150")
TAG = (b'<meta name="huelle-herkuenfte" content="https://lernen.example.test http://127.0.0.1:8150">'
       b'<script type="module" src="/__huelle/speichern.js"></script>')


def _mit_seite(url: str) -> Durchreicher:
    return Durchreicher(url, _frei(), EIGEN, rahmen_herkuenfte=SEITE_HERKUNFT)


def _kopf(headers: list[tuple[bytes, bytes]]) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in headers}


@pytest.mark.parametrize("pfad", ["/", "/index.html"])
@pytest.mark.parametrize("annahme", [b"", b"gzip, deflate, br"])
def test_html_seite_bekommt_das_skript_einmal(fake: tuple[FakePenEcho, str], pfad: str, annahme: bytes) -> None:
    upstream, url = fake
    status, headers, teile = asyncio.run(_lauf(_mit_seite(url), pfad, headers=[(b"accept-encoding", annahme)]))
    body = b"".join(t for _, t in teile)
    kopf = _kopf(headers)
    assert status == 200
    assert upstream.kopf.get("accept-encoding") == "identity"  # nie komprimiert holen
    assert body.count(b"/__huelle/speichern.js") == 1
    assert body == INDEX.replace(b"</head>", TAG + b"</head>", 1)
    assert kopf[b"content-length"] == str(len(body)).encode()
    assert b"content-encoding" not in kopf and b"etag" not in kopf
    assert kopf[b"content-type"] == b"text/html; charset=utf-8"
    # PenEchos script-src 'self' erlaubt das Skript der eigenen Herkunft; frame-ancestors wie bisher.
    csp = kopf[b"content-security-policy"].decode()
    assert "script-src 'self' https://cdn.jsdelivr.net;" in csp
    assert "frame-ancestors https://lernen.example.test http://127.0.0.1:8150" in csp


def test_einspritzen_mit_abfrage(fake: tuple[FakePenEcho, str]) -> None:
    """Der Rahmen laedt `/?lernen=1`: auch dann kommt das Speichern-Skript, und die Abfrage erreicht die Flaeche."""
    upstream, url = fake
    upstream.abfragen.clear()
    status, _, teile = asyncio.run(_lauf(_mit_seite(url), "/", query=b"lernen=1"))
    body = b"".join(t for _, t in teile)
    assert status == 200
    assert body == INDEX.replace(b"</head>", TAG + b"</head>", 1)
    assert upstream.abfragen[-1] == b"lernen=1"  # davor oeffnet der Durchreicher den lokalen Zugang


def test_html_trotz_identity_gzip_wird_entpackt(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.immer_gzip = True
    try:
        _, headers, teile = asyncio.run(_lauf(_mit_seite(url), "/"))
    finally:
        upstream.immer_gzip = False
    body = b"".join(t for _, t in teile)
    kopf = _kopf(headers)
    assert body == INDEX.replace(b"</head>", TAG + b"</head>", 1)
    assert b"content-encoding" not in kopf
    assert kopf[b"content-length"] == str(len(body)).encode()


def test_html_head_ohne_falsche_laenge(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    status, headers, teile = asyncio.run(_lauf(_mit_seite(url), "/", methode="HEAD"))
    assert status == 200 and teile == []
    assert b"content-length" not in _kopf(headers)


def test_nicht_html_bleibt_bytegleich(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    _, headers, teile = asyncio.run(_lauf(_mit_seite(url), "/app.js", headers=[(b"accept-encoding", b"gzip")]))
    assert upstream.kopf.get("accept-encoding") == "gzip"  # unveraendert weitergereicht
    kopf = _kopf(headers)
    body = b"".join(t for _, t in teile)
    assert kopf[b"content-encoding"] == b"gzip" and kopf[b"etag"] == b'W/"abc"'
    assert body == gzip.compress(SKRIPT) or gzip.decompress(body) == SKRIPT
    assert kopf[b"content-length"] == str(len(body)).encode()


def test_andere_html_seiten_bleiben_unberuehrt(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    _, _, teile = asyncio.run(_lauf(_mit_seite(url), "/widget-host.html"))
    assert b"".join(t for _, t in teile) == INDEX
    assert upstream.kopf.get("accept-encoding") is None


def test_ohne_seitenherkunft_kein_skript(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    _, _, teile = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/"))
    assert b"".join(t for _, t in teile) == INDEX


def test_ki_sperre_bleibt_423_mit_seitenherkunft(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    app = Durchreicher(url, Sperre(), EIGEN, rahmen_herkuenfte=SEITE_HERKUNFT)
    status, _, _ = asyncio.run(_lauf(app, "/api/ai/command", methode="POST", body=b"{}"))
    assert status == 423 and upstream.aufrufe == []


@pytest.mark.parametrize("methode", ["GET", "HEAD"])
def test_skript_kommt_vom_durchreicher(fake: tuple[FakePenEcho, str], methode: str) -> None:
    from spaces.learning.huelle.durchreicher import SPEICHERN_SKRIPT

    upstream, url = fake
    upstream.aufrufe.clear()
    status, headers, teile = asyncio.run(_lauf(_mit_seite(url), "/__huelle/speichern.js", methode=methode))
    kopf = _kopf(headers)
    assert status == 200 and upstream.aufrufe == []
    assert kopf[b"content-type"] == b"text/javascript; charset=utf-8"
    assert kopf[b"x-content-type-options"] == b"nosniff"
    assert kopf[b"content-length"] == str(SPEICHERN_SKRIPT.stat().st_size).encode()
    assert b"".join(t for _, t in teile) == (SPEICHERN_SKRIPT.read_bytes() if methode == "GET" else b"")


@pytest.mark.parametrize("pfad", ["/__huelle/", "/__huelle/x.js", "/__HUELLE/speichern.js", "/__huelle/../app.js"])
def test_andere_huelle_pfade_404_ohne_upstream(fake: tuple[FakePenEcho, str], pfad: str) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, _ = asyncio.run(_lauf(_mit_seite(url), pfad))
    assert status == 404 and upstream.aufrufe == []


def test_skript_nur_lesen(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, _ = asyncio.run(_lauf(_mit_seite(url), "/__huelle/speichern.js", methode="POST", body=b"x"))
    assert status == 405 and upstream.aufrufe == []


def test_einfuegen_kopf_dann_body_sonst_nichts() -> None:
    from spaces.learning.huelle.durchreicher import skript_einfuegen

    t = b"<x>"
    assert skript_einfuegen(b"<html><HEAD></HEAD><body></body>", t) == b"<html><HEAD><x></HEAD><body></body>"
    assert skript_einfuegen(b"<html><body>a</body></html>", t) == b"<html><body>a<x></body></html>"
    assert skript_einfuegen(b"kein html", t) is None
    schon = b"<head><script src=\"/__huelle/speichern.js\"></script></head>"
    assert skript_einfuegen(schon, t) is None  # nie doppelt


def test_csp_ohne_self_bekommt_self_fuer_skripte() -> None:
    from spaces.learning.huelle.durchreicher import skript_erlauben

    assert skript_erlauben("default-src 'self'; script-src 'self' https://cdn") == \
        "default-src 'self'; script-src 'self' https://cdn"
    assert skript_erlauben("script-src https://cdn; img-src data:") == "script-src https://cdn 'self'; img-src data:"
    assert skript_erlauben("script-src 'none'") == "script-src 'self'"
    assert skript_erlauben("default-src 'none'") == "default-src 'none'; script-src 'self'"
    assert skript_erlauben("default-src 'self'") == "default-src 'self'"
    assert skript_erlauben("script-src-elem https://cdn; script-src 'self'") == \
        "script-src-elem https://cdn 'self'; script-src 'self'"


@pytest.mark.parametrize("kodierung,daten", [("br", b"\x1b\x00brotli"), ("gzip", b"\x1f\x8bkaputt"),
                                             ("gzip", gzip.compress(INDEX)[:-20])])
def test_unlesbare_kompression_geht_unveraendert_weiter(kodierung: str, daten: bytes) -> None:
    app = _mit_seite("http://127.0.0.1:1")

    async def roh() -> Any:
        yield daten[:5]
        yield daten[5:]

    async def lauf() -> Any:
        antwort = httpx.Response(200, headers={"content-type": "text/html", "content-encoding": kodierung})
        return await app._seite_mit_skript(antwort, roh(), [(b"content-encoding", kodierung.encode())])

    fertig, vorab = asyncio.run(lauf())
    assert fertig is None
    assert b"".join(vorab) == daten  # nichts verloren, nichts veraendert

def test_seite_oeffnet_unentschiedenen_zugang_selbst(fake: tuple[FakePenEcho, str]) -> None:
    """Nach einem PenEcho-Neustart fragt PenEcho nach Code oder „offen“; die Huelle waehlt offen (Lernende sehen nichts)."""
    upstream, url = fake
    upstream.zugang, upstream.geoeffnet = "undecided", []
    status, _, _ = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/", headers=[(b"host", b"lernen.example.test")]))
    assert status == 200
    assert len(upstream.geoeffnet) == 1
    origin, art, body = upstream.geoeffnet[0]
    assert origin == url
    assert (art or "").startswith("application/json")
    assert json.loads(body) == {"acknowledgeRisk": True}
    assert upstream.zugang == "open"


def test_offener_zugang_wird_nicht_nochmal_geoeffnet(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.zugang, upstream.geoeffnet = "open", []
    asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/index.html"))
    assert upstream.geoeffnet == []


def test_andere_pfade_fragen_den_zugang_nicht(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.zugang, upstream.geoeffnet = "undecided", []
    upstream.aufrufe.clear()
    asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/app.js"))
    assert upstream.aufrufe == ["/app.js"] and upstream.geoeffnet == []
    upstream.zugang = "open"


# ---- Server-Ablage je Person (Abschluss-Korrektur Teil B) -------------------------------------------------

def _ablage(karte: dict[str, frozenset[str] | None]) -> Any:
    """Kennung -> documentIds wie Bruecke.eigene_dokumente; der Inhaber (None) ist nicht eingeschraenkt."""
    gefragt: list[str] = []

    def ablage(actor: str) -> frozenset[str] | None:
        gefragt.append(actor)
        return karte.get(actor, frozenset())

    ablage.gefragt = gefragt  # type: ignore[attr-defined]
    return ablage


ABLAGE = {"lh-1-inhaber": None, "lh-1-person-a": frozenset({"doc-a"}), "lh-1-person-b": frozenset({"doc-b"})}
ALS_A = {"actor_id": "lh-1-person-a"}
KOPF_A = [(b"cookie", b"huelle_person=geheim; penecho_ai_session_x=abc")]


def _body(teile: list[tuple[float, bytes]]) -> Any:
    return json.loads(b"".join(t for _, t in teile))


def _ablage_lauf(url: str, pfad: str, *, state: dict[str, Any] | None = ALS_A, **kw: Any) -> tuple[int, Any]:
    status, _, teile = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN, ablage=_ablage(ABLAGE)), pfad,
                                         state=state, headers=KOPF_A, **kw))
    return status, _body(teile)


def test_ablage_liste_nur_eigene_boards(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    status, daten = _ablage_lauf(url, "/api/canvases", query=b"metadataOnly=1")
    assert status == 200 and [b["id"] for b in daten["canvases"]] == [BOARD_A]
    status, daten = _ablage_lauf(url, "/api/canvases")  # auch ohne metadataOnly
    assert status == 200 and [b["id"] for b in daten["canvases"]] == [BOARD_A]
    # Die Cookies der Huelle gehen auch bei dieser eigenen Anfrage nie an PenEcho.
    assert upstream.kopf.get("cookie") == "penecho_ai_session_x=abc"
    # Der Inhaber (Notweg) sieht wie bisher alles.
    status, daten = _ablage_lauf(url, "/api/canvases", query=b"metadataOnly=1", state={"actor_id": "lh-1-inhaber"})
    assert status == 200 and [b["id"] for b in daten["canvases"]] == [BOARD_A, BOARD_B, BOARD_ALT]
    # Ohne Kennung im Scope: nichts (fail-closed).
    status, daten = _ablage_lauf(url, "/api/canvases", query=b"metadataOnly=1", state=None)
    assert status == 200 and daten["canvases"] == []


def test_ablage_eigenes_board_lesen_speichern_umbenennen(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    for methode, pfad in (("GET", f"/api/canvases/{BOARD_A}"), ("PUT", f"/api/canvases/{BOARD_A}"),
                          ("PATCH", f"/api/canvases/{BOARD_A}"), ("GET", f"/api/canvases/{BOARD_A}/preview")):
        upstream.ablage.clear()
        status, _ = _ablage_lauf(url, pfad, methode=methode, body=b'{"name":"x"}')
        assert status == 200, (methode, pfad)
        # Erst die Besitzpruefung (Liste lesen), dann die Anfrage selbst, mit ihrem Body.
        assert upstream.ablage[0][:3] == ("GET", "/api/canvases", b"metadataOnly=1")
        assert upstream.ablage[1] == (methode, pfad, b"", b'{"name":"x"}' if methode != "GET" else b"")


def test_ablage_neues_board_anlegen(fake: tuple[FakePenEcho, str]) -> None:
    # Erstes Speichern im Lernmodus: POST /api/canvases (PenEcho vergibt die id; eine vorhandene gibt 409).
    upstream, url = fake
    upstream.ablage.clear()
    status, _ = _ablage_lauf(url, "/api/canvases", methode="POST", body=b'{"manifest":{}}')
    assert status == 201
    assert upstream.ablage == [("POST", "/api/canvases", b"", b'{"manifest":{}}')]


def test_ablage_fremdes_board_403(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    for board in (BOARD_B, BOARD_ALT, "1700000000009-ffffffff"):  # fremd, ohne documentId, unbekannt
        for methode, pfad in (("GET", f"/api/canvases/{board}"), ("PUT", f"/api/canvases/{board}"),
                              ("PATCH", f"/api/canvases/{board}"), ("GET", f"/api/canvases/{board}/preview")):
            upstream.ablage.clear()
            status, daten = _ablage_lauf(url, pfad, methode=methode, body=b"{}")
            assert status == 403 and daten == {"error": "Nicht erlaubt."}, (methode, pfad)
            assert [a[:2] for a in upstream.ablage] == [("GET", "/api/canvases")]  # nur die Besitzpruefung


def test_ablage_was_der_lernmodus_nicht_braucht_403(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    faelle = [
        ("DELETE", f"/api/canvases/{BOARD_A}", b""),
        ("PUT", f"/api/canvases/{BOARD_A}/project", b""),
        ("GET", "/api/canvas-projects", b""), ("POST", "/api/canvas-projects", b""),
        ("DELETE", "/api/canvas-projects/project-aaaaaaaa", b""),
        ("PUT", "/api/canvases", b""), ("HEAD", f"/api/canvases/{BOARD_A}", b""),
        # Umgehungen: PenEcho (WHATWG-URL) loest .. und %2e auf; geprueft wird der rohe Pfad exakt.
        ("GET", f"/api/canvases/x/../{BOARD_B}", b""), ("GET", f"/x/../api/canvases/{BOARD_B}", b""),
        ("GET", f"/api/canvases/%2e%2e/canvases/{BOARD_B}", b""), ("GET", f"/API/canvases/{BOARD_B}", b""),
        ("GET", "/api/canvases/", b""), ("GET", f"/api/canvases/{BOARD_A}/", b""),
    ]
    for methode, pfad, query in faelle:
        upstream.ablage.clear()
        status, _ = _ablage_lauf(url, pfad, methode=methode, query=query)
        assert status == 403, (methode, pfad, query)
        assert upstream.ablage == [], (methode, pfad, query)


def test_ablage_inhaber_und_ohne_ablage_wie_bisher(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.ablage.clear()
    status, _ = _ablage_lauf(url, f"/api/canvases/{BOARD_B}", methode="DELETE", state={"actor_id": "lh-1-inhaber"})
    assert status == 200 and upstream.ablage == [("DELETE", f"/api/canvases/{BOARD_B}", b"", b"")]
    upstream.ablage.clear()
    status, _, _ = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), f"/api/canvases/{BOARD_B}", state=ALS_A))
    assert status == 200 and upstream.ablage == [("GET", f"/api/canvases/{BOARD_B}", b"", b"")]


# ---- Runde 2: Lernmodus-Allowlist fuer Personen ------------------------------------------------------------

INHABER = "lh-1-inhaber"
ALS_INHABER = {"actor_id": INHABER}


def _mit_allowlist(url: str) -> Durchreicher:
    return Durchreicher(url, _frei(), EIGEN, ablage=_ablage(ABLAGE), inhaber=INHABER)


def _http(url: str, pfad: str, *, methode: str = "GET", state: dict[str, Any] | None = ALS_A,
          query: bytes = b"") -> tuple[int, bytes]:
    status, _, teile = asyncio.run(_lauf(_mit_allowlist(url), pfad, methode=methode, state=state, query=query,
                                         headers=KOPF_A, body=b"{}" if methode not in ("GET", "HEAD") else b""))
    return status, b"".join(t for _, t in teile)


def _ws(url: str, pfad: str, *, state: dict[str, Any] | None = ALS_A, query: bytes = b"") -> list[dict[str, Any]]:
    """Websocket ueber den Durchreicher; Ergebnis: alle gesendeten Nachrichten (Fake-PenEcho antwortet mit echo)."""
    gesendet: list[dict[str, Any]] = []

    async def lauf() -> None:
        scope: dict[str, Any] = {"type": "websocket", "path": pfad, "raw_path": pfad.encode(), "query_string": query,
                                 "headers": [(b"origin", b"https://lernen.example.test"), *KOPF_A]}
        if state is not None:
            scope["state"] = state
        eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        eingang.put_nowait({"type": "websocket.connect"})

        async def send(m: dict[str, Any]) -> None:
            gesendet.append(m)
            if m["type"] == "websocket.accept":
                eingang.put_nowait({"type": "websocket.receive", "text": "hi"})
            if m["type"] == "websocket.send":
                eingang.put_nowait({"type": "websocket.disconnect", "code": 1000})

        await asyncio.wait_for(_mit_allowlist(url)(scope, eingang.get, send), timeout=10)

    asyncio.run(lauf())
    return gesendet


GESPERRT_HTTP = [
    # Canvas-Agent: Dateien und Laufwerke des Inhaber-PCs, Projekte, Verlauf, Upload
    ("GET", "/api/canvas-agent/host-roots", b""), ("GET", "/api/canvas-agent/roots", b""),
    ("GET", "/api/canvas-agent/roots/r1/entries", b"approved=1"),
    ("POST", "/api/canvas-agent/projects/from-host-root", b""), ("POST", "/api/canvas-agent/projects", b""),
    ("GET", "/api/canvas-agent/projects", b""), ("PUT", "/api/canvas-agent/projects/p1/history", b""),
    ("DELETE", "/api/canvas-agent/projects/p1", b""), ("POST", "/api/canvas-agent/files", b""),
    # Einstellungen: Anbieter, Schluessel, CLI-Pfade
    ("GET", "/api/settings", b""), ("POST", "/api/settings", b""), ("GET", "/api/settings/connections", b""),
    ("POST", "/api/settings/connections", b""), ("POST", "/api/settings/connections/inspect-cli", b""),
    ("POST", "/api/settings/connections/models", b""),
    # Notizen
    ("GET", "/api/notes", b""), ("POST", "/api/notes/sync", b""), ("DELETE", "/api/notes/n1", b""),
    # Unbekannt oder nicht im Lernmodus
    ("GET", "/api/unbekannt", b""), ("GET", "/api", b""), ("GET", "/api/v1/credits", b""),
    ("GET", "/api/widget-fetch", b"url=https://example.test"), ("POST", "/api/mcp/rpc", b""),
    ("GET", "/api/mcp/status", b""), ("POST", "/api/mcp/configure", b""), ("GET", "/api/cloud/status", b""),
    ("POST", "/api/cloud/sign-out", b""), ("POST", "/api/suggest/preferences", b""), ("POST", "/api/plugins", b""),
    ("DELETE", "/api/plugins/p1", b""), ("POST", "/api/local-access/open", b""), ("GET", "/api/favorites", b""),
    ("GET", "/api/debug/log", b""), ("POST", "/api/config", b""), ("GET", "/api/ai/command", b""),
    ("GET", "/plugins/private/p1.md", b""), ("POST", "/index.html", b""),
    # Kodierungs- und Pfad-Umgehungen
    ("GET", "/api/canvas%2Dagent/host-roots", b""), ("GET", "/api/%73ettings", b""), ("GET", "/API/settings", b""),
    ("GET", "/Api/Notes", b""), ("GET", "//api/settings", b""), ("GET", "/api//settings", b""),
    ("GET", "/x/../api/settings", b""), ("GET", "/api/./settings", b""), ("GET", "/api/config.js/../settings", b""),
    ("POST", "/api/ai/command/../../api/settings", b""), ("GET", "/%2e%2e/api/settings", b""),
    ("GET", "/plugins/private/../private/p1.md", b""), ("GET", "/Plugins/Private/p1.md", b""),
]


@pytest.mark.parametrize(("methode", "pfad", "query"), GESPERRT_HTTP)
def test_person_gesperrte_routen_403(fake: tuple[FakePenEcho, str], methode: str, pfad: str, query: bytes) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    upstream.ablage.clear()
    status, body = _http(url, pfad, methode=methode, query=query)
    assert status == 403 and body.decode() == "Nicht erlaubt."
    assert upstream.aufrufe == [] and upstream.ablage == []


@pytest.mark.parametrize(("methode", "pfad", "query"), [f for f in GESPERRT_HTTP if "%" not in f[1]
                                                        and "/../" not in f[1] and "//" not in f[1]])
def test_inhaber_gesperrte_routen_wie_bisher(fake: tuple[FakePenEcho, str], methode: str, pfad: str,
                                             query: bytes) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _ = _http(url, pfad, methode=methode, query=query, state=ALS_INHABER)
    assert status in (200, 201) and upstream.aufrufe, (methode, pfad)


def test_ohne_kennung_gilt_die_allowlist(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    assert _http(url, "/api/settings", state=None)[0] == 403
    assert _http(url, "/widget-renderer.js", state=None)[0] == 200  # oeffentliche Datei der Weiche
    assert upstream.aufrufe == ["/widget-renderer.js"]


def test_ohne_inhaber_keine_allowlist(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    status, _, _ = asyncio.run(_lauf(Durchreicher(url, _frei(), EIGEN), "/api/settings", state=ALS_A))
    assert status == 200 and upstream.aufrufe == ["/api/settings"]


def test_person_lernmodus_routen_gehen_durch(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    for methode, pfad, query in [("GET", "/", b"lernen=1&flaeche=lf-x"), ("GET", "/index.html", b""),
                                 ("GET", "/app.js", b""), ("HEAD", "/app.js", b""), ("GET", "/style.css", b""),
                                 ("GET", "/vendor/mathjax-3.2.2/es5/tex-svg.js", b""), ("GET", "/quelltext", b""),
                                 ("GET", "/widget-host.html", b""), ("GET", "/api/config.js", b""),
                                 ("GET", "/api/config", b""), ("POST", "/api/ai/command", b""),
                                 ("POST", "/api/suggest", b""), ("GET", "/api/suggest/status", b""),
                                 ("GET", "/api/plugins", b""), ("GET", "/__huelle/speichern.js", b"")]:
        status, _ = _http(url, pfad, methode=methode, query=query)
        assert status == 200, (methode, pfad)
    # Eigenes Board ueber die Ablage-Pruefung
    upstream.ablage.clear()
    assert _http(url, f"/api/canvases/{BOARD_A}")[0] == 200
    assert upstream.ablage[-1][:2] == ("GET", f"/api/canvases/{BOARD_A}")
    assert _http(url, "/api/canvases", methode="POST")[0] == 201


def test_person_ki_sperre_bleibt_davor(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.aufrufe.clear()
    gesperrt = Durchreicher(url, Sperre(), EIGEN, ablage=_ablage(ABLAGE), inhaber=INHABER)
    status, _, _ = asyncio.run(_lauf(gesperrt, "/api/ai/command", methode="POST", state=ALS_A, body=b"{}"))
    assert status == 423 and upstream.aufrufe == []


def test_person_websockets_nur_mcp_und_abgleich(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gesendet = _ws(url, "/api/mcp/canvas")
    assert gesendet[0]["type"] == "websocket.accept"
    assert {"type": "websocket.send", "text": "echo:hi"} in gesendet
    # /api/abgleich geht nur noch als Gruppenflaeche mit Ausweis durch (Teil C, siehe unten); ohne Gruppen 1008.
    assert _ws(url, "/api/abgleich", query=b"raum=r1") == [{"type": "websocket.close", "code": 1008}]
    for pfad in ("/api/canvas-agent/socket", "/api/canvas-agent/socket/", "/api/mcp/cloud-canvas", "/api/x",
                 "/api/mcp/canvas/", "/API/mcp/canvas", "/api/mcp/%63anvas", "/x/../api/canvas-agent/socket", "/"):
        assert _ws(url, pfad) == [{"type": "websocket.close", "code": 1008}], pfad
    assert _ws(url, "/api/canvas-agent/socket", state=None) == [{"type": "websocket.close", "code": 1008}]


def test_inhaber_websocket_canvas_agent_wie_bisher(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gesendet = _ws(url, "/api/canvas-agent/socket", state=ALS_INHABER)
    assert gesendet[0]["type"] == "websocket.accept"


@pytest.mark.parametrize("query", [b"limit", b"limit=", b"limit=24&offset=0", b"metadataOnly=1&limit=5", b""])
def test_ablage_liste_query_wird_metadataonly(fake: tuple[FakePenEcho, str], query: bytes) -> None:
    # ?limit lieferte ueber die Bibliotheksseite Seiten- und Projektzahlen der ganzen Ablage.
    upstream, url = fake
    upstream.ablage.clear()
    status, daten = _ablage_lauf(url, "/api/canvases", query=query)
    assert status == 200 and daten == {"canvases": [BOARDS[0]]}
    assert upstream.ablage == [("GET", "/api/canvases", b"metadataOnly=1", b"")]


# ---- Teil C: Gruppenflaeche, Mitgliedschaft und Ausweis beim Abgleich ----------------------------------------

ABGLEICH_SCHLUESSEL = "a" * 40
JETZT = 1_800_000_000.0
FENSTER = "f-1a2b"


class FakeGruppen:
    """Wie `Gruppen`: Mitgliedschaften je (actor, gid); `weg` -> UebersichtWeg; `gemerkt` wie nach einem Ausfall."""

    def __init__(self) -> None:
        self.mitglieder: dict[tuple[str, int], Mitgliedschaft] = {}
        self.gemerkte: dict[tuple[str, int], Mitgliedschaft] = {}
        self.weg = False
        self.fragen: list[tuple[str, int, bool]] = []

    def aufnehmen(self, actor: str, gid: int, name: str = "Anna Ä.") -> None:
        self.mitglieder[(actor, gid)] = Mitgliedschaft("55555555-5555-4555-8555-555555555555", "Montag", name, True)

    def mitglied(self, actor: str, gruppe_id: int, *, frisch: bool = False) -> Mitgliedschaft:
        self.fragen.append((actor, gruppe_id, frisch))
        if self.weg:
            raise UebersichtWeg()
        m = self.mitglieder.get((actor, gruppe_id))
        if m is None:
            raise KeinMitglied()
        return m

    def gemerkt(self, actor: str, gruppe_id: int) -> Mitgliedschaft | None:
        return self.gemerkte.get((actor, gruppe_id))


class Wecker:
    """Statt asyncio.sleep: schlaeft, bis der Test die Uhr vorspult."""

    def __init__(self) -> None:
        self.dauern: list[float] = []
        self._klingel: asyncio.Event | None = None

    def _event(self) -> asyncio.Event:
        if self._klingel is None:
            self._klingel = asyncio.Event()
        return self._klingel

    async def schlafen(self, sekunden: float) -> None:
        self.dauern.append(sekunden)
        klingel = self._event()
        await klingel.wait()
        klingel.clear()

    def vorspulen(self) -> None:
        self._event().set()


def _mit_gruppen(url: str, gruppen: Any, wecker: Wecker | None = None) -> Durchreicher:
    return Durchreicher(url, _frei(), EIGEN, ablage=_ablage(ABLAGE), inhaber=INHABER, gruppen=gruppen,
                        abgleich_schluessel=ABGLEICH_SCHLUESSEL, uhr=lambda: JETZT,
                        schlafen=(wecker or Wecker()).schlafen)


async def _bis(bedingung: Any, sekunden: float = 5.0) -> None:
    for _ in range(int(sekunden / 0.01)):
        if bedingung():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Bedingung nicht erreicht")


def _abfrage(roh: bytes) -> dict[str, list[str]]:
    from urllib.parse import parse_qs
    return parse_qs(roh.decode(), keep_blank_values=True, strict_parsing=True)


def _abgleich_ws(app: Durchreicher, query: bytes, *, state: dict[str, Any] | None = ALS_A,
                 pfad: str = "/api/abgleich") -> list[dict[str, Any]]:
    return _ws_app(app, pfad, query=query, state=state)


def _ws_app(app: Durchreicher, pfad: str, *, query: bytes = b"", state: dict[str, Any] | None = ALS_A,
            ) -> list[dict[str, Any]]:
    gesendet: list[dict[str, Any]] = []

    async def lauf() -> None:
        scope: dict[str, Any] = {"type": "websocket", "path": pfad, "raw_path": pfad.encode(), "query_string": query,
                                 "headers": [(b"origin", b"https://lernen.example.test"), *KOPF_A]}
        if state is not None:
            scope["state"] = state
        eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        eingang.put_nowait({"type": "websocket.connect"})

        async def send(m: dict[str, Any]) -> None:
            gesendet.append(m)
            if m["type"] == "websocket.accept":
                eingang.put_nowait({"type": "websocket.receive", "text": "hi"})
            if m["type"] == "websocket.send":
                eingang.put_nowait({"type": "websocket.disconnect", "code": 1000})

        await asyncio.wait_for(app(scope, eingang.get, send), timeout=10)

    asyncio.run(lauf())
    return gesendet


def test_abgleich_query_wird_ersetzt(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    gruppen = FakeGruppen()
    gruppen.aufnehmen("lh-1-person-a", 7)
    upstream.ws_ziele.clear()
    query = f"flaeche=g-7&fenster={FENSTER}&person=fremd&ausweis=x&name=Mallory".encode()
    gesendet = _abgleich_ws(_mit_gruppen(url, gruppen), query)
    assert gesendet[0]["type"] == "websocket.accept"
    assert {"type": "websocket.send", "text": "echo:hi"} in gesendet
    [(pfad, roh)] = upstream.ws_ziele
    assert pfad == "/api/abgleich"
    q = _abfrage(roh)
    assert list(q) == ["flaeche", "person", "fenster", "ausweis"]  # alles andere verworfen
    person = person_kennung("lh-1-person-a")
    assert q["flaeche"] == ["g-7"] and q["person"] == [person] and q["fenster"] == [FENSTER]
    # Gueltig: Flaeche, Person, Name aus der Mitgliedschaft, Ablauf jetzt + 120 s
    assert q["ausweis"] == [ausweis(ABGLEICH_SCHLUESSEL, "g-7", person, "Anna Ä.", int(JETZT) + 120)]
    assert gruppen.fragen == [("lh-1-person-a", 7, False)]


def test_abgleich_inhaber_braucht_auch_mitgliedschaft(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    gruppen = FakeGruppen()
    query = f"flaeche=g-7&fenster={FENSTER}".encode()
    assert _abgleich_ws(_mit_gruppen(url, gruppen), query, state=ALS_INHABER)[-1] == {
        "type": "websocket.close", "code": 1008, "reason": KEIN_MITGLIED}
    gruppen.aufnehmen(INHABER, 7, name="")
    upstream.ws_ziele.clear()
    gesendet = _abgleich_ws(_mit_gruppen(url, gruppen), query, state=ALS_INHABER)
    assert gesendet[0]["type"] == "websocket.accept"
    q = _abfrage(upstream.ws_ziele[0][1])
    assert q["person"] == [person_kennung(INHABER)]
    assert q["ausweis"] == [ausweis(ABGLEICH_SCHLUESSEL, "g-7", person_kennung(INHABER), "", int(JETZT) + 120)]


def test_abgleich_ohne_mitgliedschaft_1008(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    gruppen = FakeGruppen()
    gruppen.aufnehmen("lh-1-person-a", 8)  # andere Gruppe
    upstream.ws_ziele.clear()
    gesendet = _abgleich_ws(_mit_gruppen(url, gruppen), f"flaeche=g-7&fenster={FENSTER}".encode())
    assert gesendet == [{"type": "websocket.close", "code": 1008, "reason": KEIN_MITGLIED}]
    assert upstream.ws_ziele == []


def test_abgleich_learnhouse_weg_nur_mit_gemerkter_mitgliedschaft(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    gruppen = FakeGruppen()
    gruppen.weg = True
    upstream.ws_ziele.clear()
    query = f"flaeche=g-7&fenster={FENSTER}".encode()
    assert _abgleich_ws(_mit_gruppen(url, gruppen), query) == [
        {"type": "websocket.close", "code": 1008, "reason": GRUPPE_WEG}]
    assert upstream.ws_ziele == []
    gruppen.gemerkte[("lh-1-person-a", 7)] = Mitgliedschaft("55555555-5555-4555-8555-555555555555", "M", "Anna", True)
    gesendet = _abgleich_ws(_mit_gruppen(url, gruppen), query)
    assert gesendet[0]["type"] == "websocket.accept"
    assert _abfrage(upstream.ws_ziele[0][1])["ausweis"] == [
        ausweis(ABGLEICH_SCHLUESSEL, "g-7", person_kennung("lh-1-person-a"), "Anna", int(JETZT) + 120)]


async def _offene_abgleich_sitzung(app: Durchreicher, schritt: Any) -> list[dict[str, Any]]:
    """Verbindet, wartet auf das Echo von "hi" und ruft dann `schritt(eingang, gesendet)` (async)."""
    gesendet: list[dict[str, Any]] = []
    scope: dict[str, Any] = {"type": "websocket", "path": "/api/abgleich", "raw_path": b"/api/abgleich",
                             "query_string": f"flaeche=g-7&fenster={FENSTER}".encode(), "state": dict(ALS_A),
                             "headers": [(b"origin", b"https://lernen.example.test")]}
    eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    eingang.put_nowait({"type": "websocket.connect"})

    async def send(m: dict[str, Any]) -> None:
        gesendet.append(m)

    aufgabe = asyncio.ensure_future(app(scope, eingang.get, send))
    await _bis(lambda: any(m["type"] == "websocket.accept" for m in gesendet))
    eingang.put_nowait({"type": "websocket.receive", "text": "hi"})
    await _bis(lambda: {"type": "websocket.send", "text": "echo:hi"} in gesendet)
    await schritt(eingang, gesendet)
    await asyncio.wait_for(aufgabe, timeout=10)
    return gesendet


def test_abgleich_schliesst_nach_entfernen(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gruppen, wecker = FakeGruppen(), Wecker()
    gruppen.aufnehmen("lh-1-person-a", 7)

    async def entfernen(eingang: asyncio.Queue[dict[str, Any]], gesendet: list[dict[str, Any]]) -> None:
        await _bis(lambda: wecker.dauern == [60])
        del gruppen.mitglieder[("lh-1-person-a", 7)]
        wecker.vorspulen()  # 60 s spaeter
        await _bis(lambda: gesendet[-1]["type"] == "websocket.close")
        eingang.put_nowait({"type": "websocket.receive", "text": "nach-dem-entfernen"})

    gesendet = asyncio.run(_offene_abgleich_sitzung(_mit_gruppen(url, gruppen, wecker), entfernen))
    schliessen = [m for m in gesendet if m["type"] == "websocket.close"]
    assert schliessen == [{"type": "websocket.close", "code": 1008, "reason": ZUGANG_WEG}]
    assert not any(m.get("text") == "echo:nach-dem-entfernen" for m in gesendet)
    # Die Nachpruefung fragt frisch (am 60-s-Puffer vorbei)
    assert gruppen.fragen == [("lh-1-person-a", 7, False), ("lh-1-person-a", 7, True)]


def test_abgleich_nachpruefung_learnhouse_weg_bleibt_offen(fake: tuple[FakePenEcho, str]) -> None:
    _, url = fake
    gruppen, wecker = FakeGruppen(), Wecker()
    gruppen.aufnehmen("lh-1-person-a", 7)

    async def ausfall(eingang: asyncio.Queue[dict[str, Any]], gesendet: list[dict[str, Any]]) -> None:
        await _bis(lambda: wecker.dauern == [60])
        gruppen.weg = True
        wecker.vorspulen()
        await _bis(lambda: wecker.dauern == [60, 60])  # nachgeprueft, schlaeft wieder
        eingang.put_nowait({"type": "websocket.receive", "text": "weiter"})
        await _bis(lambda: {"type": "websocket.send", "text": "echo:weiter"} in gesendet)
        gruppen.weg = False
        wecker.vorspulen()  # wieder erreichbar, weiter Mitglied
        await _bis(lambda: wecker.dauern == [60, 60, 60])
        eingang.put_nowait({"type": "websocket.disconnect", "code": 1000})

    gesendet = asyncio.run(_offene_abgleich_sitzung(_mit_gruppen(url, gruppen, wecker), ausfall))
    assert not any(m["type"] == "websocket.close" and m.get("code") == 1008 for m in gesendet)
    assert [f[2] for f in gruppen.fragen] == [False, True, True]


@pytest.mark.parametrize("query", [
    b"", b"raum=r1", b"flaeche=lf-x&fenster=f-1", b"flaeche=g-7", b"fenster=f-1",
    b"flaeche=g-07&fenster=f-1", b"flaeche=g-7&fenster=F%21", b"flaeche=g-7&fenster=",
    b"flaeche=g-7&flaeche=g-8&fenster=f-1", b"flaeche=g-7&fenster=f-1&fenster=f-2", b"flaeche=g-1234567890&fenster=f-1",
    b"flaeche=g-7%0A&fenster=f-1", b"flaeche=g-7&fenster=f-1%FF",
])
def test_abgleich_ohne_gueltige_gruppenflaeche_1008(fake: tuple[FakePenEcho, str], query: bytes) -> None:
    # Solo verbindet sich nie mit /api/abgleich; alles ausser flaeche=g-<id> und fenster ist fail-closed.
    upstream, url = fake
    gruppen = FakeGruppen()
    gruppen.aufnehmen("lh-1-person-a", 7)
    gruppen.aufnehmen("lh-1-person-a", 8)
    upstream.ws_ziele.clear()
    for state in (ALS_A, ALS_INHABER):
        assert _abgleich_ws(_mit_gruppen(url, gruppen), query, state=state) == [
            {"type": "websocket.close", "code": 1008}], (query, state)
    assert upstream.ws_ziele == [] and gruppen.fragen == []


@pytest.mark.parametrize("pfad", ["/api/abgleich/", "/API/abgleich", "/api/./abgleich", "/x/../api/abgleich",
                                  "/api//abgleich", "/api/%61bgleich", "/api/abgleich/x"])
def test_abgleich_pfadvarianten_1008_auch_fuer_den_inhaber(fake: tuple[FakePenEcho, str], pfad: str) -> None:
    upstream, url = fake
    gruppen = FakeGruppen()
    gruppen.aufnehmen(INHABER, 7)
    upstream.ws_ziele.clear()
    gesendet = _abgleich_ws(_mit_gruppen(url, gruppen), f"flaeche=g-7&fenster={FENSTER}".encode(),
                            state=ALS_INHABER, pfad=pfad)
    assert gesendet == [{"type": "websocket.close", "code": 1008}], pfad
    assert upstream.ws_ziele == []


def test_abgleich_ohne_kennung_1008(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.ws_ziele.clear()
    app = Durchreicher(url, _frei(), EIGEN, gruppen=FakeGruppen(), abgleich_schluessel=ABGLEICH_SCHLUESSEL)
    assert _abgleich_ws(app, f"flaeche=g-7&fenster={FENSTER}".encode(), state=None) == [
        {"type": "websocket.close", "code": 1008}]
    assert upstream.ws_ziele == []


def test_gruppen_aus_abgleich_1008(fake: tuple[FakePenEcho, str]) -> None:
    # Ohne Gruppen-Einstellung (kein Gruppen-Objekt oder kein Schluessel) gibt es keinen Abgleich, auch fuer den Inhaber.
    upstream, url = fake
    gruppen = FakeGruppen()
    gruppen.aufnehmen(INHABER, 7)
    upstream.ws_ziele.clear()
    query = f"flaeche=g-7&fenster={FENSTER}".encode()
    for app in (Durchreicher(url, _frei(), EIGEN, inhaber=INHABER),
                Durchreicher(url, _frei(), EIGEN, inhaber=INHABER, gruppen=gruppen, abgleich_schluessel="")):
        assert _abgleich_ws(app, query, state=ALS_INHABER) == [{"type": "websocket.close", "code": 1008}]
    assert upstream.ws_ziele == [] and gruppen.fragen == []


def test_andere_websockets_unberuehrt_von_gruppen(fake: tuple[FakePenEcho, str]) -> None:
    upstream, url = fake
    upstream.ws_ziele.clear()
    gesendet = _ws_app(_mit_gruppen(url, FakeGruppen()), "/api/mcp/canvas", query=b"x=1&person=p")
    assert gesendet[0]["type"] == "websocket.accept"
    assert upstream.ws_ziele == [("/api/mcp/canvas", b"x=1&person=p")]


def test_abgleich_log_ohne_geheimnisse(fake: tuple[FakePenEcho, str], caplog: pytest.LogCaptureFixture) -> None:
    _, url = fake
    gruppen = FakeGruppen()
    gruppen.aufnehmen("lh-1-person-a", 7)
    with caplog.at_level(logging.DEBUG, logger="spaces.learning.huelle"):
        _abgleich_ws(_mit_gruppen(url, gruppen), f"flaeche=g-7&fenster={FENSTER}".encode())
        gruppen.weg = True
        _abgleich_ws(_mit_gruppen(url, gruppen), f"flaeche=g-7&fenster={FENSTER}".encode())
    for geheim in (ABGLEICH_SCHLUESSEL, "lh-1-person-a", person_kennung("lh-1-person-a"), "Anna"):
        assert geheim not in caplog.text
