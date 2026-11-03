"""PenEcho-MCP-Client der Huelle gegen einen Fake-JSON-RPC-Server."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import httpx
import pytest

from spaces.learning.huelle.penecho_mcp import PenEchoMcp, PenEchoNichtErreichbar

TOKEN = "ab" * 32
SID = "s" * 64


class Fake:
    def __init__(self, *, fehler: bool = False) -> None:
        self.anfragen: list[tuple[dict, httpx.Headers]] = []
        self.fehler = fehler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.anfragen.append((body, request.headers))
        if request.headers.get("authorization") != "Bearer " + TOKEN:
            return httpx.Response(401, json={})
        methode = body["method"]
        if methode == "notifications/initialized":
            return httpx.Response(202)
        if methode == "initialize":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}},
                headers={"mcp-session-id": SID},
            )
        if self.fehler:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -1, "message": "x"}})
        name = body["params"]["name"]
        if name == "penecho_start_session":
            res = {"structuredContent": {"sessionId": "sess-1"}, "content": []}
        elif name == "penecho_capture_canvas":
            png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 8).decode()
            res = {"content": [{"type": "image", "data": png, "mimeType": "image/png"}]}
        else:
            res = {"structuredContent": {"applied": True}, "content": []}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": res})

    def aufrufe(self, name: str) -> list[dict]:
        return [b["params"] for b, _ in self.anfragen
                if b["method"] == "tools/call" and b["params"]["name"] == name]


@pytest.fixture
def zustand(tmp_path: Path) -> Path:
    z = tmp_path / "penecho"
    (z / "direct-http").mkdir(parents=True)
    (z / "direct-http" / "identity.json").write_text(
        json.dumps({"accessToken": TOKEN, "certificatePem": "unbenutzt-im-mock"}), encoding="utf-8")
    return z


def _client(zustand: Path, fake: Fake) -> PenEchoMcp:
    return PenEchoMcp(zustand, transport=httpx.MockTransport(fake))


def test_initialize_dann_session_header(zustand: Path) -> None:
    fake = Fake()
    assert _client(zustand, fake).sitzung("Kurs A") == "sess-1"
    methoden = [b["method"] for b, _ in fake.anfragen]
    assert methoden[:2] == ["initialize", "notifications/initialized"]
    assert fake.anfragen[0][1].get("mcp-session-id") is None
    for _, kopf in fake.anfragen[1:]:
        assert kopf["mcp-session-id"] == SID


def test_token_aus_identity_und_nicht_im_log(zustand: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = Fake(fehler=True)
    c = _client(zustand, fake)
    with pytest.raises(PenEchoNichtErreichbar) as info:
        c.sitzung("Kurs A")
    assert TOKEN not in caplog.text
    assert TOKEN not in str(info.value)
    # und der Token ging tatsaechlich als Bearer raus (Fake lehnt sonst mit 401 ab)
    assert any(h.get("authorization") == "Bearer " + TOKEN for _, h in fake.anfragen)


def test_aufgabe_setzen_koordinaten(zustand: Path) -> None:
    fake = Fake()
    _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "Rechne 2+2", 3)
    arg = fake.aufrufe("penecho_edit_canvas")[0]["arguments"]
    assert arg["action"] == "create_text"
    assert arg["text"] == "Aufgabe 3: Rechne 2+2"
    assert arg["region"]["x"] == 0 and arg["region"]["y"] == 4800
    assert arg["sessionId"] == "sess-1" and arg["requestId"]


def test_notizkarte_artifact_id(zustand: Path) -> None:
    fake = Fake()
    _client(zustand, fake).notizkarte("sess-1", "t1", "Rückmeldung", "Gut gemacht.")
    arg = fake.aufrufe("penecho_present_widget")[0]["arguments"]
    assert arg["artifactId"] == "rueckmeldung-t1"
    assert arg["title"] == "Rückmeldung" and "note" in arg and "html" not in arg


def test_sitzung_wird_gemerkt(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    assert c.sitzung("Kurs A") == "sess-1"
    assert c.sitzung("Kurs A") == "sess-1"
    assert len(fake.aufrufe("penecho_start_session")) == 1
    assert fake.aufrufe("penecho_start_session")[0]["arguments"]["restore"] is True
    datei = zustand.parent / "learning-huelle" / "sitzungen.json"
    assert json.loads(datei.read_text(encoding="utf-8")) == {"Kurs A": "sess-1"}
    # neue Instanz (Neustart der Huelle) startet ebenfalls keine neue Sitzung
    fake2 = Fake()
    assert _client(zustand, fake2).sitzung("Kurs A") == "sess-1"
    assert fake2.aufrufe("penecho_start_session") == []


def test_fehler_wird_nicht_erreichbar(zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(fehler=True)).aufgabe_setzen("s", "t", "x", 1)

    def kaputt(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nein")

    with pytest.raises(PenEchoNichtErreichbar):
        PenEchoMcp(zustand, transport=httpx.MockTransport(kaputt)).sitzung("K")


def test_identity_fehlt_oder_zertifikat_ungueltig(tmp_path: Path, zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar):
        PenEchoMcp(tmp_path / "leer").sitzung("K")
    with pytest.raises(PenEchoNichtErreichbar):  # echter TLS-Pfad, Zertifikat kein PEM
        PenEchoMcp(zustand).sitzung("K")


def test_schnappschuss_liefert_png(zustand: Path) -> None:
    fake = Fake()
    daten = _client(zustand, fake).schnappschuss("sess-1", 2)
    assert daten.startswith(b"\x89PNG")
    arg = fake.aufrufe("penecho_capture_canvas")[0]["arguments"]
    assert arg["target"] == "region"
    assert arg["region"] == {"x": 0, "y": 3200, "w": 2400, "h": 1600}
