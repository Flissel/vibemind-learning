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


def test_belegter_bereich_setzt_aufgabe_an_freien_platz(zustand: Path) -> None:
    # PenEchos KI darf nach der Abgabe frei zeichnen und kann dabei den naechsten
    # Aufgabenbereich belegen (LAYOUT_CONFLICT). Dann ohne festen Bereich setzen.
    fake = Fake()
    echt = fake.__call__

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call" and "region" in body["params"]["arguments"]:
            fake.anfragen.append((body, request.headers))
            sc = {"code": "LAYOUT_CONFLICT", "message": "m"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": True, "structuredContent": sc}})
        return echt(request)

    PenEchoMcp(zustand, transport=httpx.MockTransport(antwort)).aufgabe_setzen("sess-1", "t1", "Rechne", 2)
    versuche = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert len(versuche) == 2
    assert versuche[0]["region"] == bereich(2) and "region" not in versuche[1]
    assert versuche[1]["text"] == "Aufgabe 2: Rechne"
    # Andere Argumente, also eine andere requestId (PenEcho: nur fuer identische Argumente wiederverwenden).
    assert versuche[0]["requestId"] != versuche[1]["requestId"]


@pytest.mark.parametrize("code", ["STORAGE_UNAVAILABLE", "DOCUMENT_NOT_FOUND"])
def test_verlorene_flaeche_bekommt_neuen_schluessel(zustand: Path, code: str) -> None:
    # Ungesicherte PenEcho-Flaechen ueberleben keinen Neustart; die alte Bindung laesst sich
    # nicht wiederherstellen. Dann eine neue Flaeche unter einem neuen Schluessel anlegen.
    fake = Fake()
    echt = fake.__call__

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call" and body["params"]["arguments"].get("sessionKey") == "learning-huelle:" + LERN_SID:
            fake.anfragen.append((body, request.headers))
            sc = {"code": code, "message": "m"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": True, "structuredContent": sc}})
        return echt(request)

    c = PenEchoMcp(zustand, transport=httpx.MockTransport(antwort))
    assert c.sitzung(LERN_SID, TITEL) == "sess-1"
    schluessel = [a["arguments"]["sessionKey"] for a in fake.aufrufe("penecho_start_session")]
    assert schluessel[0] == "learning-huelle:" + LERN_SID
    assert len(schluessel) == 2 and schluessel[1].startswith("learning-huelle:" + LERN_SID + "#")
    assert len(schluessel[1]) <= 128
    # Nach Neustart der Huelle (PenEcho kennt die sessionId nicht mehr) dieselbe neue Flaeche
    # wiederherstellen, statt erneut eine anzulegen.
    c2 = PenEchoMcp(zustand, transport=httpx.MockTransport(antwort))
    c2.vergessen(LERN_SID)
    c2.sitzung(LERN_SID, TITEL)
    assert [a["arguments"]["sessionKey"] for a in fake.aufrufe("penecho_start_session")][2] == schluessel[1]


# ---- Board speichern und wieder oeffnen (BOARD_SPEICHERN.md) --------------------------

DOK = "doc-" + "d" * 64
DOKUMENTE = "dokumente.json"


def _fehler(body: dict, code: str) -> httpx.Response:
    sc = {"code": code, "message": "m"}
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
        "isError": True, "structuredContent": sc, "content": [{"type": "text", "text": json.dumps(sc)}]}})


def _ok(body: dict, sc: dict) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
        "structuredContent": sc, "content": [{"type": "text", "text": json.dumps(sc)}]}})


def _merkdatei(zustand: Path, name: str) -> dict:
    datei = zustand.parent / "learning-huelle" / name
    return json.loads(datei.read_text(encoding="utf-8")) if datei.exists() else {}


def test_speichern_ruft_das_gefundene_werkzeug(zustand: Path) -> None:
    fake = Fake()
    echt = fake.__call__

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call" and body["params"]["name"] == "penecho_inspect_session":
            fake.anfragen.append((body, request.headers))
            return _ok(body, {"sessionId": "sess-1", "documentId": DOK, "canvasId": "cv-1"})
        return echt(request)

    c = PenEchoMcp(zustand, transport=httpx.MockTransport(antwort))
    sid = c.sitzung(LERN_SID, TITEL)
    vorher = len([b for b, _ in fake.anfragen if b["method"] == "tools/call"])
    c.speichern(sid)
    aufrufe = [b["params"] for b, _ in fake.anfragen if b["method"] == "tools/call"][vorher:]
    assert aufrufe == [{"name": "penecho_inspect_session", "arguments": {"sessionId": "sess-1"}}]
    # Die Dokument-ID ist zum Lernsitzungs-Schluessel gemerkt, neben flaechen.json.
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: DOK}
    c.speichern(sid)  # wiederholbar
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: DOK}


def test_speichern_fehler_kommen_als_nicht_erreichbar(zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(tool_fehler="session_not_found")).speichern("alt")
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(fehler=True)).speichern("sess-1")
    # Antwort ohne Dokument-ID: nichts merken, Fehler melden.
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake()).speichern("sess-1")
    assert _merkdatei(zustand, DOKUMENTE) == {}


def _verlust_fake(fake: Fake, *, browser_mit_dokument: str | None, open_fehler: str = "STORAGE_UNAVAILABLE",
                  start_mit_dokument_fehler: str | None = None):
    """start_session ohne documentId: Flaeche verloren. open_canvas klappt nur im genannten Browser."""
    echt = fake.__call__

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") != "tools/call":
            return echt(request)
        name, args = body["params"]["name"], body["params"]["arguments"]
        if name == "penecho_start_session":
            fake.anfragen.append((body, request.headers))
            if args.get("documentId") == DOK:
                if start_mit_dokument_fehler:
                    return _fehler(body, start_mit_dokument_fehler)
                return _ok(body, {"sessionId": "sess-2", "documentId": DOK, "reused": True})
            if args.get("sessionKey") == "learning-huelle:" + LERN_SID:
                return _fehler(body, "STORAGE_UNAVAILABLE")
            return _ok(body, {"sessionId": "sess-neu", "documentId": "doc-neu"})
        if name == "penecho_list_canvases":
            fake.anfragen.append((body, request.headers))
            return _ok(body, {"instanceId": "inst-1", "canvases": [
                {"instanceId": "inst-1", "canvasId": "cv-a", "documentId": "doc-x", "title": "x", "active": True},
                {"instanceId": "inst-1", "canvasId": "cv-a", "documentId": "doc-y", "title": "y", "active": False},
                {"instanceId": "inst-1", "canvasId": "cv-b", "documentId": "doc-z", "title": "z", "active": True},
            ]})
        if name == "penecho_open_canvas":
            fake.anfragen.append((body, request.headers))
            if args["canvasId"] == browser_mit_dokument and args.get("documentId") == DOK:
                return _ok(body, {"documentId": DOK, "title": "t", "active": False})
            return _fehler(body, open_fehler)
        return echt(request)

    return antwort


def _dokument_merken(zustand: Path) -> None:
    datei = zustand.parent / "learning-huelle" / DOKUMENTE
    datei.parent.mkdir(parents=True, exist_ok=True)
    datei.write_text(json.dumps({LERN_SID: DOK}), encoding="utf-8")


def test_sitzung_oeffnet_gespeichertes_board_nach_verlust(zustand: Path) -> None:
    _dokument_merken(zustand)
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_verlust_fake(fake, browser_mit_dokument="cv-b")))
    assert c.sitzung(LERN_SID, TITEL) == "sess-2"
    # Jeder verbundene Browser wird einmal gefragt, bis einer das Dokument oeffnet.
    oeffnen = [a["arguments"] for a in fake.aufrufe("penecho_open_canvas")]
    assert [a["canvasId"] for a in oeffnen] == ["cv-a", "cv-b"]
    for a in oeffnen:
        assert a["documentId"] == DOK and a["instanceId"] == "inst-1"
        assert {"instanceId", "canvasId", "requestId"} <= set(a) <= {
            "instanceId", "canvasId", "documentId", "locator", "create", "title", "requestId", "show"}
        assert 1 <= len(a["requestId"]) <= 128
    assert oeffnen[0]["requestId"] != oeffnen[1]["requestId"]
    starts = [a["arguments"] for a in fake.aufrufe("penecho_start_session")]
    assert len(starts) == 2
    assert all(s["sessionKey"] == "learning-huelle:" + LERN_SID for s in starts)
    assert "documentId" not in starts[0] and starts[1]["documentId"] == DOK
    # Kein neuer Ersatzschluessel, keine leere Flaeche.
    assert _merkdatei(zustand, "flaechen.json") == {}
    assert _merkdatei(zustand, "sitzungen.json") == {LERN_SID: "sess-2"}
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: DOK}


def test_sitzung_ohne_gespeichertes_board_wie_bisher(zustand: Path) -> None:
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_verlust_fake(fake, browser_mit_dokument="cv-b")))
    assert c.sitzung(LERN_SID, TITEL) == "sess-neu"
    assert fake.aufrufe("penecho_list_canvases") == [] and fake.aufrufe("penecho_open_canvas") == []
    starts = [a["arguments"]["sessionKey"] for a in fake.aufrufe("penecho_start_session")]
    assert len(starts) == 2 and starts[1].startswith("learning-huelle:" + LERN_SID + "#")
    assert _merkdatei(zustand, "flaechen.json") == {LERN_SID: starts[1]}
    # Die neue Flaeche ist ab jetzt das Board dieser Lernsitzung.
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: "doc-neu"}


def test_sitzung_gespeichertes_board_in_keinem_browser_dann_ersatz(zustand: Path) -> None:
    _dokument_merken(zustand)
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_verlust_fake(fake, browser_mit_dokument=None)))
    assert c.sitzung(LERN_SID, TITEL) == "sess-neu"
    assert len(fake.aufrufe("penecho_open_canvas")) == 2
    starts = [a["arguments"]["sessionKey"] for a in fake.aufrufe("penecho_start_session")]
    assert starts[-1].startswith("learning-huelle:" + LERN_SID + "#")
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: "doc-neu"}


def test_sitzung_anderer_fehler_in_einem_browser_sucht_im_naechsten(zustand: Path) -> None:
    # Ein Browser antwortet mit einem anderen Fehler (z. B. beschaeftigt): weiter zum naechsten.
    _dokument_merken(zustand)
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(
        _verlust_fake(fake, browser_mit_dokument="cv-b", open_fehler="CANVAS_BUSY")))
    assert c.sitzung(LERN_SID, TITEL) == "sess-2"
    assert [a["arguments"]["canvasId"] for a in fake.aufrufe("penecho_open_canvas")] == ["cv-a", "cv-b"]
    assert _merkdatei(zustand, "flaechen.json") == {}


@pytest.mark.parametrize("code", ["STORAGE_UNAVAILABLE", "session_key_conflict"])
def test_sitzung_bindung_an_gefundenes_board_scheitert_dann_ersatz(zustand: Path, code: str) -> None:
    # open_canvas findet das Dokument, aber die Bindung daran schlaegt fehl: Ersatzflaeche statt Abbruch.
    _dokument_merken(zustand)
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(
        _verlust_fake(fake, browser_mit_dokument="cv-b", start_mit_dokument_fehler=code)))
    assert c.sitzung(LERN_SID, TITEL) == "sess-neu"
    starts = [a["arguments"]["sessionKey"] for a in fake.aufrufe("penecho_start_session")]
    assert len(starts) == 3 and starts[-1].startswith("learning-huelle:" + LERN_SID + "#")
    assert _merkdatei(zustand, "flaechen.json") == {LERN_SID: starts[-1]}
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: "doc-neu"}


def test_werkzeugfehler_loggt_penechos_code_aber_keinen_text(zustand: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(tool_fehler="LAYOUT_CONFLICT")).aufgabe_setzen("s", "t", "x", 1)
    assert "penecho_edit_canvas meldet Fehler LAYOUT_CONFLICT" in caplog.text
    # Freitext oder ungewoehnliche Codes landen nicht im Log.
    caplog.clear()
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(tool_fehler="geheim mit Leerzeichen")).aufgabe_setzen("s", "t", "x", 1)
    assert "geheim" not in caplog.text and "meldet Fehler ?" in caplog.text


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
