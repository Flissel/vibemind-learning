"""PenEcho-MCP-Client der Huelle gegen einen Fake-JSON-RPC-Server."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import httpx
import pytest

from spaces.learning.huelle.penecho_mcp import (
    BEREICHE, FLAECHE_GROESSE, PenEchoMcp, PenEchoNichtErreichbar, PenEchoSitzungUnbekannt, bereich,
)

TOKEN = "ab" * 32
SID = "s" * 64
LERN_SID = "33333333-3333-4333-8333-333333333333"
TITEL = "Kurs A · 03.11.2026"


class Fake:
    def __init__(self, *, fehler: bool = False, tool_404: int = 0, tool_fehler: str | None = None) -> None:
        self.tool_404 = tool_404
        self.tool_fehler = tool_fehler
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
        if self.tool_404 > 0:
            self.tool_404 -= 1
            return httpx.Response(404, json={"error": "Session not found"})
        name = body["params"]["name"]
        if self.tool_fehler:
            sc = {"code": self.tool_fehler, "message": "m"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "isError": True, "structuredContent": sc, "content": [{"type": "text", "text": json.dumps(sc)}]}})
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
    assert _client(zustand, fake).sitzung(LERN_SID, TITEL) == "sess-1"
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
        c.sitzung(LERN_SID, TITEL)
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
    assert arg["region"] == bereich(3)
    # Deterministisch: dieselbe Aufgabe am selben Platz ist dieselbe Anfrage (PenEcho-Idempotenz).
    assert arg["sessionId"] == "sess-1" and arg["requestId"] == "aufgabe-t1-3"


def test_notizkarte_artifact_id(zustand: Path) -> None:
    fake = Fake()
    _client(zustand, fake).notizkarte("sess-1", "t1", "Rückmeldung", "Gut gemacht.")
    arg = fake.aufrufe("penecho_present_widget")[0]["arguments"]
    assert arg["artifactId"] == "rueckmeldung-t1"
    assert arg["title"] == "Rückmeldung" and "note" in arg and "html" not in arg


def test_sitzung_wird_gemerkt(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    assert c.sitzung(LERN_SID, TITEL) == "sess-1"
    assert c.sitzung(LERN_SID, "anderer Titel") == "sess-1"
    assert len(fake.aufrufe("penecho_start_session")) == 1
    argumente = fake.aufrufe("penecho_start_session")[0]["arguments"]
    assert argumente["restore"] is True and argumente["title"] == TITEL
    assert argumente["sessionKey"] == "learning-huelle:" + LERN_SID
    datei = zustand.parent / "learning-huelle" / "sitzungen.json"
    assert json.loads(datei.read_text(encoding="utf-8")) == {LERN_SID: "sess-1"}
    # neue Instanz (Neustart der Huelle) startet ebenfalls keine neue Sitzung
    fake2 = Fake()
    assert _client(zustand, fake2).sitzung(LERN_SID, TITEL) == "sess-1"
    assert fake2.aufrufe("penecho_start_session") == []
    # Eine neue Lernsitzung (auch desselben Kurses) bekommt eine eigene Flaeche.
    c.sitzung("44444444-4444-4444-8444-444444444444", TITEL)
    assert len(fake.aufrufe("penecho_start_session")) == 2


def test_fehler_wird_nicht_erreichbar(zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(fehler=True)).aufgabe_setzen("s", "t", "x", 1)

    def kaputt(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nein")

    with pytest.raises(PenEchoNichtErreichbar):
        PenEchoMcp(zustand, transport=httpx.MockTransport(kaputt)).sitzung(LERN_SID, "K")


def test_identity_fehlt_oder_zertifikat_ungueltig(tmp_path: Path, zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar):
        PenEchoMcp(tmp_path / "leer").sitzung(LERN_SID, "K")
    with pytest.raises(PenEchoNichtErreichbar):  # echter TLS-Pfad, Zertifikat kein PEM
        PenEchoMcp(zustand).sitzung(LERN_SID, "K")


def test_schnappschuss_liefert_png(zustand: Path) -> None:
    fake = Fake()
    daten = _client(zustand, fake).schnappschuss("sess-1", 2)
    assert daten.startswith(b"\x89PNG")
    arg = fake.aufrufe("penecho_capture_canvas")[0]["arguments"]
    assert arg["target"] == "region"
    assert arg["region"] == bereich(2)


def test_404_auf_werkzeugaufruf_reconnect_und_einmal_wiederholen(zustand: Path) -> None:
    fake = Fake(tool_404=1)
    _client(zustand, fake).aufgabe_setzen("sess-1", "t", "x", 1)
    methoden = [b["method"] for b, _ in fake.anfragen]
    assert methoden.count("initialize") == 2
    assert len(fake.aufrufe("penecho_edit_canvas")) == 2  # 404-Versuch + Wiederholung
    neue = [b for b, _ in fake.anfragen if b["method"] == "tools/call"]
    assert len(neue) == 2 and neue[0]["params"]["arguments"]["requestId"] == neue[1]["params"]["arguments"]["requestId"]


def test_zweites_404_wird_nicht_erreichbar(zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(tool_404=2)).aufgabe_setzen("sess-1", "t", "x", 1)


def test_vergessen_entfernt_eintrag(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    c.sitzung(LERN_SID, TITEL)
    c.vergessen(LERN_SID)
    datei = zustand.parent / "learning-huelle" / "sitzungen.json"
    assert json.loads(datei.read_text(encoding="utf-8")) == {}
    c.sitzung(LERN_SID, TITEL)
    assert len(fake.aufrufe("penecho_start_session")) == 2
    c.vergessen("Unbekannt")  # kein Fehler


@pytest.mark.parametrize("code", ["session_not_found", "session_expired", "session_closed", "canvas_disconnected"])
def test_unbekannte_sitzung_ist_eigener_fehler(zustand: Path, code: str) -> None:
    with pytest.raises(PenEchoSitzungUnbekannt) as info:
        _client(zustand, Fake(tool_fehler=code)).aufgabe_setzen("alt", "t", "x", 1)
    assert isinstance(info.value, PenEchoNichtErreichbar)


def test_anderer_werkzeugfehler_bleibt_allgemein(zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar) as info:
        _client(zustand, Fake(tool_fehler="invalid_argument")).aufgabe_setzen("s", "t", "x", 1)
    assert not isinstance(info.value, PenEchoSitzungUnbekannt)


# ---- Ruling L: eine Flaeche je Lernsitzung, 20 Bereiche in PenEchos Grenzen -------------

def test_bereiche_liegen_in_penechos_grenzen_und_ueberlappen_nicht() -> None:
    assert FLAECHE_GROESSE == 20000 and BEREICHE == 20  # PenEcho schema.js: Weltkoordinaten 0..20000
    flaechen = [bereich(n) for n in range(1, BEREICHE + 1)]
    for r in flaechen:
        assert r["x"] >= 0 and r["y"] >= 0
        assert r["x"] + r["w"] <= FLAECHE_GROESSE and r["y"] + r["h"] <= FLAECHE_GROESSE
        assert (r["w"], r["h"]) == (9000, 1900)
    for i, a in enumerate(flaechen):
        for b in flaechen[i + 1:]:
            getrennt = (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                        or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])
            assert getrennt, (a, b)


@pytest.mark.parametrize("nummer", [0, -1, 21, 100, True])
def test_bereich_ausserhalb_wirft(nummer: int) -> None:
    with pytest.raises(ValueError):
        bereich(nummer)


def test_aufgabe_ausserhalb_der_bereiche_ruft_penecho_nicht(zustand: Path) -> None:
    fake = Fake()
    with pytest.raises(ValueError):
        _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "x", 21)
    with pytest.raises(ValueError):
        _client(zustand, fake).schnappschuss("sess-1", 21)
    assert fake.aufrufe("penecho_edit_canvas") == [] and fake.aufrufe("penecho_capture_canvas") == []


def test_sitzungsschluessel_hoechstens_128_zeichen(zustand: Path) -> None:
    fake = Fake()
    _client(zustand, fake).sitzung("x" * 300, TITEL)
    schluessel = fake.aufrufe("penecho_start_session")[0]["arguments"]["sessionKey"]
    assert len(schluessel) <= 128 and schluessel.startswith("learning-huelle:")


# Pflichtschluessel je Werkzeug, kopiert aus PenEcho 1.4.2 src/server/mcp/schema.js:
#   penecho_start_session  Z.658 required:["title"]
#   penecho_edit_canvas    Z.665 required:["sessionId","requestId","action"]
#   penecho_present_widget Z.675 required:["sessionId","artifactId","title"] + Z.703 requestId
#   penecho_capture_canvas Z.666 required:["sessionId"] (kein requestId: exactKeys Z.474 lehnt ihn ab)
_PFLICHT = {
    "penecho_start_session": {"title"},
    "penecho_edit_canvas": {"sessionId", "requestId", "action"},
    "penecho_present_widget": {"sessionId", "artifactId", "title", "requestId"},
    "penecho_capture_canvas": {"sessionId"},
}
_ERLAUBT = {
    "penecho_start_session": {"canvasId", "instanceId", "documentId", "target", "takeover", "title",
                              "client", "sessionKey", "restore", "show"},
    "penecho_capture_canvas": {"sessionId", "target", "objectId", "artifactId", "region", "quality"},
}


def test_alle_aufrufe_enthalten_die_pflichtschluessel_des_schemas(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    c.sitzung(LERN_SID, TITEL)
    c.aufgabe_setzen("sess-1", "t1", "x", 1)
    c.notizkarte("sess-1", "t1", "Rueckmeldung", "Gut.")
    c.schnappschuss("sess-1", 1)
    for name, pflicht in _PFLICHT.items():
        aufrufe = fake.aufrufe(name)
        assert aufrufe, name
        for a in aufrufe:
            assert pflicht <= set(a["arguments"]), name
            assert not set(a["arguments"]) - _ERLAUBT.get(name, set(a["arguments"])), name


def test_widget_request_id_deterministisch_und_gueltig(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    c.notizkarte("sess-1", "t1", "A", "Text")
    c.notizkarte("sess-1", "t1", "A", "Text")
    c.notizkarte("sess-1", "t1", "A", "anderer Text")
    c.notizkarte("sess-1", "x" * 200, "A", "Text")
    ids = [a["arguments"]["requestId"] for a in fake.aufrufe("penecho_present_widget")]
    assert ids[0] == ids[1] != ids[2]  # gleiche Argumente gleiche Id, geaenderte Argumente neue Id
    assert ids[0].startswith("rueckmeldung-t1")
    assert all(1 <= len(i) <= 128 for i in ids)
