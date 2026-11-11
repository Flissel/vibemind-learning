"""PenEcho-MCP-Client der Huelle gegen einen Fake-JSON-RPC-Server."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import httpx
import pytest

from spaces.learning.huelle.penecho_mcp import (
    BEREICHE, FLAECHE_GROESSE, Aufgabenbild, PenEchoAnfrageSchonBekannt, PenEchoMcp, PenEchoNichtErreichbar,
    PenEchoFlaecheVoll, PenEchoPlatzBelegt, PenEchoSitzungUnbekannt, arbeitsfeld, bereich, bildfeld, ergebnisfeld, rueckmeldungsfeld,
    berichtsplatz, bildraster, felder, textfeld,
)
from spaces.learning.huelle.anordnung import anordnung, markenzeile, zeile

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
        elif name == "penecho_edit_canvas" and body["params"]["arguments"].get("action") == "create_text":
            res = {"structuredContent": {"applied": True, "objectId": "text-box-1"}, "content": []}
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
    assert arg["region"] == textfeld(3)
    # Deterministisch: dieselbe Aufgabe am selben Platz ist dieselbe Anfrage (PenEcho-Idempotenz).
    assert arg["sessionId"] == "sess-1" and arg["requestId"] == "aufgabe-t1-3"


def test_nach_der_aufgabe_zeigt_penecho_sie(zustand: Path) -> None:
    # Eine Spalte im nativen Massstab: ab Aufgabe 2 liegt der Text unter dem Bildschirmrand.
    fake = Fake()
    _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "Rechne", 5)
    edits = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert [e["action"] for e in edits] == ["create_text", "show"]
    assert edits[1]["objectId"] == "text-box-1" and "baseRevision" not in edits[1]
    assert set(edits[1]) == {"sessionId", "requestId", "action", "objectId"}


def test_zeigen_fehler_bricht_die_aufgabe_nicht_ab(zustand: Path, caplog: pytest.LogCaptureFixture) -> None:
    fake = Fake()
    echt = fake.__call__

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call" and body["params"]["arguments"].get("action") == "show":
            sc = {"code": "OBJECT_NOT_FOUND", "message": "m"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": True, "structuredContent": sc}})
        return echt(request)

    with caplog.at_level(logging.WARNING):
        PenEchoMcp(zustand, transport=httpx.MockTransport(antwort)).aufgabe_setzen("sess-1", "t1", "x", 2)
    assert "Aufgabe nicht gezeigt" in caplog.text


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


def test_schnappschuss_zeitlimit_geht_an_den_http_aufruf_ohne_wiederholung(zustand: Path) -> None:
    fake = Fake(tool_404=1)
    zeiten: list[tuple[str, float | None]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body.get("params", {}).get("name") if body["method"] == "tools/call" else None
        zeiten.append((name or body["method"], request.extensions["timeout"]["read"]))
        return fake(request)

    c = PenEchoMcp(zustand, transport=httpx.MockTransport(transport))
    with pytest.raises(PenEchoNichtErreichbar):
        c.schnappschuss("sess-1", 1, timeout=5.0)  # 404 -> mit Zeitlimit kein zweiter Versuch
    aufnahmen = [t for n, t in zeiten if n == "penecho_capture_canvas"]
    assert aufnahmen == [5.0]
    assert [t for n, t in zeiten if n == "initialize"] == [60.0]  # Vorgabe bleibt fuer den Rest
    fake2 = Fake()
    zeiten.clear()
    c2 = PenEchoMcp(zustand, transport=httpx.MockTransport(lambda r: (zeiten.append(r.extensions["timeout"]["read"]), fake2(r))[1]))
    assert c2.schnappschuss("sess-1", 1).startswith(b"PNG", 1)
    assert set(zeiten) == {60.0}


def test_404_auf_werkzeugaufruf_reconnect_und_einmal_wiederholen(zustand: Path) -> None:
    # Ueber sitzung(): aufgabe_setzen liest seit F18 zuerst (optional) die Belegung.
    fake = Fake(tool_404=1)
    assert _client(zustand, fake).sitzung(LERN_SID, TITEL) == "sess-1"
    methoden = [b["method"] for b, _ in fake.anfragen]
    assert methoden.count("initialize") == 2
    starts = fake.aufrufe("penecho_start_session")
    assert len(starts) == 2  # 404-Versuch + Wiederholung
    assert starts[0]["arguments"] == starts[1]["arguments"]


def test_zweites_404_wird_nicht_erreichbar(zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(tool_404=2)).sitzung(LERN_SID, TITEL)


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
    versuche = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas") if a["arguments"]["action"] == "create_text"]
    assert len(versuche) == 2
    assert versuche[0]["region"] == textfeld(2) and "region" not in versuche[1]
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


def test_dokument_merken_ruft_inspect_und_merkt_die_id(zustand: Path) -> None:
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
    assert c.dokument_merken(sid) is True
    aufrufe = [b["params"] for b, _ in fake.anfragen if b["method"] == "tools/call"][vorher:]
    assert aufrufe == [{"name": "penecho_inspect_session", "arguments": {"sessionId": "sess-1"}}]
    # Die Dokument-ID ist zum Lernsitzungs-Schluessel gemerkt, neben flaechen.json.
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: DOK}
    assert c.dokument_merken(sid) is True  # wiederholbar
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: DOK}


def test_dokument_merken_fehler_kommen_als_nicht_erreichbar(zustand: Path) -> None:
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(tool_fehler="session_not_found")).dokument_merken("alt")
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake(fehler=True)).dokument_merken("sess-1")
    # Antwort ohne Dokument-ID: nichts merken, Fehler melden.
    with pytest.raises(PenEchoNichtErreichbar):
        _client(zustand, Fake()).dokument_merken("sess-1")
    assert _merkdatei(zustand, DOKUMENTE) == {}


def test_dokument_merken_fremde_sitzung_gibt_false(zustand: Path) -> None:
    """PenEcho kennt die Sitzung, die Huelle hat dazu keine Lernsitzung: False, nichts gemerkt."""
    fake = Fake()
    echt = fake.__call__

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call" and body["params"]["name"] == "penecho_inspect_session":
            return _ok(body, {"sessionId": "fremd", "documentId": DOK, "canvasId": "cv-1"})
        return echt(request)

    c = PenEchoMcp(zustand, transport=httpx.MockTransport(antwort))
    c.sitzung(LERN_SID, TITEL)  # eigene Sitzung sess-1 ist gemerkt, "fremd" nicht
    assert c.dokument_merken("fremd") is False
    assert _merkdatei(zustand, DOKUMENTE) == {}


def test_speichern_gibt_es_nicht_mehr() -> None:
    # Der Server kann nicht speichern (BOARD_SPEICHERN.md); der alte Name versprach das.
    assert not hasattr(PenEchoMcp, "speichern")


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
        assert (r["w"], r["h"]) == (1300, 800)  # nativer Massstab: PenEcho setzt Text mit 20 px
    belegt = flaechen + [rueckmeldungsfeld(n) for n in range(1, BEREICHE + 1)]
    for r in belegt:
        assert r["x"] + r["w"] <= FLAECHE_GROESSE and r["y"] + r["h"] <= FLAECHE_GROESSE
    for i, a in enumerate(belegt):
        for b in belegt[i + 1:]:
            assert _getrennt(a, b), (a, b)


def _getrennt(a: dict, b: dict) -> bool:
    return a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"]


def _innen(innen: dict, aussen: dict) -> bool:
    return (innen["x"] >= aussen["x"] and innen["y"] >= aussen["y"]
            and innen["x"] + innen["w"] <= aussen["x"] + aussen["w"] and innen["y"] + innen["h"] <= aussen["y"] + aussen["h"])


@pytest.mark.parametrize("nummer", range(1, 21))
def test_textfeld_im_bereich_neben_dem_ergebnisfeld(nummer: int) -> None:
    t, f = textfeld(nummer), ergebnisfeld(nummer)
    assert _innen(t, bereich(nummer))
    assert t["w"] >= 400  # PenEcho bricht Text bei 400 um (canvas-documents.js maxWidth)
    # Rahmen (8) und Schild (56 darueber) ueberlappen den Text nicht.
    assert _getrennt(t, {"x": f["x"] - 16, "y": f["y"] - 60, "w": f["w"] + 32, "h": f["h"] + 76})


@pytest.mark.parametrize("nummer", range(1, 21))
def test_rueckmeldung_liegt_rechts_neben_dem_bereich(nummer: int) -> None:
    # Ausserhalb des Bereichs: der Schnappschuss offener Aufgaben enthaelt nur die Arbeit der Lernenden.
    b, r = bereich(nummer), rueckmeldungsfeld(nummer)
    assert r["x"] >= b["x"] + b["w"] and r["y"] == b["y"] + 20 and r["w"] >= 400 and r["y"] + r["h"] <= b["y"] + b["h"]


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
    c.bericht_zeigen("sess-1", LERN_SID, "# Bericht", ())
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
    c.bericht_zeigen("sess-1", "t1", "# A\nText", ())
    c.bericht_zeigen("sess-1", "t1", "# A\nText", ())
    c.bericht_zeigen("sess-1", "t1", "# A\nanderer Text", ())
    c.bericht_zeigen("sess-1", "x" * 200, "# A\nText", ())
    ids = [a["arguments"]["requestId"] for a in fake.aufrufe("penecho_present_widget")]
    assert ids[0] == ids[1] != ids[2]  # gleiche Argumente gleiche Id, geaenderte Argumente neue Id
    assert ids[0].startswith("bericht-t1")
    assert all(1 <= len(i) <= 128 for i in ids)


# ---- Task 6: Ergebnisfeld, Rahmen, Rueckmeldung ---------------------------------------------

class FeldFake(Fake):
    """Wie Fake, beantwortet penecho_read_file mit canvas.json und einer Revision."""

    def __init__(self, revision: int = 7) -> None:
        super().__init__()
        self.revision = revision
        self.signaturen: dict[str, str] = {}  # requestId -> Argumente, wie PenEchos mutationRequests
        self.gezeichnet = 0

    def konflikt(self, body: dict) -> httpx.Response | None:
        """PenEcho (bound-operations.js): gleiche requestId mit anderen Argumenten ist REQUEST_ID_CONFLICT.

        Der Eintrag entsteht vor dem Browseraufruf und bleibt auch nach einem Fehler stehen.
        """
        args = body["params"]["arguments"]
        if body["params"]["name"] != "penecho_edit_canvas":
            return None
        signatur = json.dumps(args, sort_keys=True)
        if self.signaturen.setdefault(args["requestId"], signatur) != signatur:
            sc = {"code": "REQUEST_ID_CONFLICT", "message": "m"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "isError": True, "structuredContent": sc, "content": []}})
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "tools/call":
            abgelehnt = self.konflikt(body)
            if abgelehnt is not None:
                self.anfragen.append((body, request.headers))
                return abgelehnt
            if body["params"]["arguments"].get("action") == "draw_ink":
                self.gezeichnet += 1
        if body["method"] == "tools/call" and body["params"]["name"] == "penecho_read_file":
            self.anfragen.append((body, request.headers))
            sc = {"content": json.dumps({"revision": self.revision}), "contentHash": "h"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"structuredContent": sc, "content": []}})
        return super().__call__(request)


def _ink(fake: Fake) -> list[dict]:
    return [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas") if a["arguments"]["action"] == "draw_ink"]


def _fehler_bei_ink(fake: FeldFake, code: str, wie_oft: int = 1) -> httpx.MockTransport:
    original = fake.__call__
    zaehler = {"n": 0}

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "tools/call" and body["params"]["arguments"].get("action") == "draw_ink":
            zaehler["n"] += 1
            abgelehnt = fake.konflikt(body)
            if abgelehnt is not None:
                fake.anfragen.append((body, request.headers))
                return abgelehnt
            if zaehler["n"] <= wie_oft:
                fake.anfragen.append((body, request.headers))
                fake.revision += 1
                sc = {"code": code, "message": "m"}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                    "isError": True, "structuredContent": sc, "content": []}})
        return original(request)

    return httpx.MockTransport(antwort)


@pytest.mark.parametrize("nummer", range(1, 21))
def test_ergebnisfeld_liegt_im_bereich(nummer: int) -> None:
    b, f = bereich(nummer), ergebnisfeld(nummer)
    assert f["w"] >= 360 and f["h"] >= 140
    # Rahmen (8 aussen) und Schild (60 darueber) bleiben ebenfalls im Bereich.
    assert f["x"] - 16 >= b["x"] and f["x"] + f["w"] + 16 <= b["x"] + b["w"]
    assert f["y"] - 60 >= b["y"] and f["y"] + f["h"] + 16 <= b["y"] + b["h"]
    # oben rechts: darunter bleibt Platz fuer Rechenweg oder Skizze
    assert f["x"] >= b["x"] + b["w"] / 2 and f["y"] + f["h"] <= b["y"] + b["h"] / 2


def test_ergebnisfeld_ausserhalb_wirft() -> None:
    with pytest.raises(ValueError):
        ergebnisfeld(21)


def test_aufgabe_setzen_ohne_ergebnisfeld_bleibt_wie_bisher(zustand: Path) -> None:
    fake = FeldFake()
    _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "x", 3)
    assert [a["arguments"]["action"] for a in fake.aufrufe("penecho_edit_canvas")] == ["create_text", "show"]
    assert fake.aufrufe("penecho_read_file") == []


def test_aufgabe_setzen_mit_ergebnisfeld_zwei_aufrufe_feste_ids(zustand: Path) -> None:
    fake = FeldFake(revision=7)
    c = _client(zustand, fake)
    c.aufgabe_setzen("sess-1", "t1", "x", 3, mit_ergebnisfeld=True)
    c.aufgabe_setzen("sess-1", "t1", "x", 3, mit_ergebnisfeld=True)
    edits = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas") if a["arguments"]["action"] != "show"]
    assert len(edits) == 4
    erste, zweite = edits[:2], edits[2:]
    assert [e["requestId"] for e in erste] == [e["requestId"] for e in zweite]
    assert len({e["requestId"] for e in erste}) == 2
    rahmen = [e for e in erste if e["action"] == "draw_ink"]
    assert len(rahmen) == 1 and rahmen[0]["requestId"] == "ergebnis-t1-3"
    assert rahmen[0]["baseRevision"] == 7
    (text,) = [e for e in erste if e["action"] == "create_text"]
    assert text["text"] == "Aufgabe 3: x\n\nIhr Ergebnis schreiben Sie in das blau umrandete Feld."


def test_aufgaben_bleiben_unter_penechos_textgrenze(zustand: Path) -> None:
    # Abnahme 06.11. (F15): PenEcho nimmt je Flaeche hoechstens 50 Texte (canvasDocumentsCapacity,
    # auch MAX_VISIBLE_TEXT_BOXES fuer die Lernenden). Mit Feld-Schild brauchte jede Aufgabe 3 Texte,
    # bei Aufgabe 17 kam OBJECT_LIMIT. Aufgabentext + Rueckmeldung = 2 je Aufgabe, 20 Aufgaben = 40.
    fake = FeldFake()
    c = _client(zustand, fake)
    for n in range(1, 21):
        c.aufgabe_setzen("sess-1", f"t{n}", "x", n, mit_ergebnisfeld=n % 2 == 0, mit_arbeitsfeld=n % 2 == 1)
        c.rueckmeldung_zeigen("sess-1", f"t{n}", n, gelesen=None, urteil="Richtig.", text="Gut.")
    texte = [a for a in fake.aufrufe("penecho_edit_canvas") if a["arguments"]["action"] == "create_text"]
    assert len(texte) <= 40


def test_rahmen_ist_tinte_um_das_feld_ohne_widget(zustand: Path) -> None:
    fake = FeldFake()
    _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "x", 12, mit_ergebnisfeld=True)
    arg = _ink(fake)[0]
    f = ergebnisfeld(12)
    punkte = [p for s in arg["strokes"] for p in s["points"]]
    xs, ys = [p["x"] for p in punkte], [p["y"] for p in punkte]
    assert max(xs) - min(xs) <= 2048 and max(ys) - min(ys) <= 2048
    # Tinte liegt ausserhalb des Feldes, der Schnappschuss enthaelt nur Handschrift.
    assert min(xs) < f["x"] and max(xs) > f["x"] + f["w"] and min(ys) < f["y"] and max(ys) > f["y"] + f["h"]
    assert all(s["points"][0] == s["points"][-1] for s in arg["strokes"])
    assert fake.aufrufe("penecho_draw") == [] and fake.aufrufe("penecho_present_widget") == []


def test_rahmen_veraltete_revision_wird_einmal_neu_gelesen(zustand: Path) -> None:
    fake = FeldFake(revision=1)
    transport = _fehler_bei_ink(fake, "REVISION_CONFLICT")
    PenEchoMcp(zustand, transport=transport).aufgabe_setzen("s", "t", "x", 1, mit_ergebnisfeld=True)
    ink = _ink(fake)
    assert [i["baseRevision"] for i in ink] == [1, 2]  # nach dem Konflikt wird canvas.json neu gelesen
    # Neue Revision braucht neue Kennung, sonst lehnt PenEcho mit REQUEST_ID_CONFLICT ab.
    assert ink[0]["requestId"] == "ergebnis-t-1" and ink[1]["requestId"] == "ergebnis-t-1-r2"
    assert len(ink[1]["requestId"]) <= 128
    assert fake.gezeichnet == 1  # der Rahmen steht wirklich (zweiter Versuch von PenEcho angenommen)


def test_rahmen_dauerhaft_veraltet_gibt_auf_ohne_fehler(zustand: Path, caplog: pytest.LogCaptureFixture) -> None:
    fake = FeldFake()
    transport = _fehler_bei_ink(fake, "REVISION_CONFLICT", wie_oft=5)
    with caplog.at_level(logging.WARNING):
        PenEchoMcp(zustand, transport=transport).aufgabe_setzen("s", "t", "x", 1, mit_ergebnisfeld=True)
    assert len(_ink(fake)) == 2  # genau ein Wiederholungsversuch
    assert "Revision veraltet" in caplog.text and fake.gezeichnet == 0


def test_rahmen_bereits_gezeichnet_ist_kein_fehler(zustand: Path) -> None:
    """Gleiche requestId, andere Revision: PenEcho meldet REQUEST_ID_CONFLICT, der Rahmen steht schon."""
    fake = FeldFake()
    PenEchoMcp(zustand, transport=_fehler_bei_ink(fake, "REQUEST_ID_CONFLICT")).aufgabe_setzen(
        "s", "t", "x", 1, mit_ergebnisfeld=True)
    assert len(_ink(fake)) == 1


def test_sitzung_unbekannt_beim_rahmen_wird_nicht_verschluckt(zustand: Path) -> None:
    fake = FeldFake()
    with pytest.raises(PenEchoSitzungUnbekannt):
        PenEchoMcp(zustand, transport=_fehler_bei_ink(fake, "session_closed")).aufgabe_setzen(
            "s", "t", "x", 1, mit_ergebnisfeld=True)


def test_ergebnis_schnappschuss_ist_png_des_feldes(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    assert c.ergebnis_schnappschuss("sess-1", 5).startswith(b"\x89PNG")
    arg = fake.aufrufe("penecho_capture_canvas")[0]["arguments"]
    assert arg["target"] == "region" and arg["region"] == ergebnisfeld(5) and "requestId" not in arg
    with pytest.raises(ValueError):
        c.ergebnis_schnappschuss("sess-1", 21)


def test_urteile_sind_genau_fuenf() -> None:
    from spaces.learning.huelle.penecho_mcp import URTEILE

    assert URTEILE == ("Richtig.", "Teilweise richtig.", "Noch nicht richtig.", "Nicht lesbar.", "Wird noch geprüft.")


@pytest.mark.parametrize("urteil", ["Richtig.", "Teilweise richtig.", "Noch nicht richtig.", "Nicht lesbar.",
                                    "Wird noch geprüft."])
def test_rueckmeldung_zeigen_inhalt(zustand: Path, urteil: str) -> None:
    fake = Fake()
    _client(zustand, fake).rueckmeldung_zeigen("sess-1", "t1", 4, gelesen="B", urteil=urteil, text="Gut gemacht.")
    arg = fake.aufrufe("penecho_edit_canvas")[0]["arguments"]
    assert arg["action"] == "create_text" and arg["region"] == rueckmeldungsfeld(4)
    zeilen = arg["text"].splitlines()
    assert zeilen[0] == "Rückmeldung zu Aufgabe 4" and zeilen[2] == "Gelesen: B"
    assert urteil in arg["text"] and "Gut gemacht." in arg["text"]
    assert fake.aufrufe("penecho_present_widget") == []


def test_rueckmeldung_ohne_gelesen_hat_keine_gelesen_zeile(zustand: Path) -> None:
    fake = Fake()
    _client(zustand, fake).rueckmeldung_zeigen(
        "sess-1", "t1", 4, gelesen=None, urteil="Nicht lesbar.", text="Bitte deutlicher schreiben.")
    inhalt = fake.aufrufe("penecho_edit_canvas")[0]["arguments"]["text"]
    assert "Gelesen" not in inhalt and inhalt.split("\n\n")[1].startswith("Nicht lesbar.")


def test_rueckmeldung_zeigen_ersetzt_gleiche_karte(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    c.rueckmeldung_zeigen("sess-1", "t1", 4, gelesen="A", urteil="Noch nicht richtig.", text="Erste.")
    c.rueckmeldung_zeigen("sess-1", "t1", 4, gelesen="B", urteil="Richtig.", text="Zweite.")
    a1, a2 = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert a1["region"] == a2["region"] == rueckmeldungsfeld(4)
    assert a1["requestId"] != a2["requestId"]  # neuer Inhalt, neue requestId
    assert a1["requestId"].startswith("rueckmeldung-t1-4-") and len(a1["requestId"]) <= 128
    c.rueckmeldung_zeigen("sess-1", "x" * 200, 4, gelesen="B", urteil="Richtig.", text="Zweite.")
    assert len(fake.aufrufe("penecho_edit_canvas")[-1]["arguments"]["requestId"]) <= 128


def _belegt_durch(fake: Fake, hindernisse: list[dict], wie_oft: int = 1) -> httpx.MockTransport:
    """Die ersten `wie_oft` Rueckmeldungs-Texte scheitern mit LAYOUT_CONFLICT und diesen Hindernissen."""
    echt = fake.__call__
    zaehler = {"n": 0}

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        args = body.get("params", {}).get("arguments", {}) if body.get("method") == "tools/call" else {}
        if args.get("action") == "create_text" and args.get("requestId", "").startswith("rueckmeldung-"):
            zaehler["n"] += 1
            if zaehler["n"] <= wie_oft:
                fake.anfragen.append((body, request.headers))
                sc = {"code": "LAYOUT_CONFLICT", "message": "m", "details": {"obstacles": hindernisse}}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                    "isError": True, "structuredContent": sc, "content": []}})
        return echt(request)

    return httpx.MockTransport(antwort)


def test_neue_rueckmeldung_ersetzt_die_alte_am_selben_platz(zustand: Path) -> None:
    fake = FeldFake(revision=9)
    r = rueckmeldungsfeld(4)
    alt = {"id": "text-box-7", "x": r["x"], "y": r["y"], "w": 380, "h": 120}
    PenEchoMcp(zustand, transport=_belegt_durch(fake, [alt])).rueckmeldung_zeigen(
        "sess-1", "t1", 4, gelesen="B", urteil="Richtig.", text="Zweite.")
    edits = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert [e["action"] for e in edits] == ["create_text", "delete", "create_text"]
    assert edits[1]["objectId"] == "text-box-7" and edits[1]["baseRevision"] == 9
    assert edits[2] == edits[0]  # gleiche requestId: PenEcho hat den Fehlversuch nicht abgeschlossen


@pytest.mark.parametrize(("objekt", "feld_von", "dx", "dy", "hoehe"), [
    ("ink:3,1", 4, 0, 0, 80),       # Handschrift der Lernenden im Rueckmeldungsfeld
    ("text-box-2", 3, 0, 0, 900),   # lange Rueckmeldung der vorigen Aufgabe ragt hinein
    ("text-box-5", 4, 120, 30, 80),  # eigener Text der Lernenden
    ("widget-1", 4, 0, 0, 80),
])
def test_fremdes_hindernis_wird_nicht_geloescht(zustand: Path, objekt: str, feld_von: int, dx: int, dy: int,
                                                hoehe: int) -> None:
    fake = FeldFake()
    r = rueckmeldungsfeld(feld_von)
    h = {"id": objekt, "x": r["x"] + dx, "y": r["y"] + dy, "w": 200, "h": hoehe}
    with pytest.raises(PenEchoPlatzBelegt):
        PenEchoMcp(zustand, transport=_belegt_durch(fake, [h])).rueckmeldung_zeigen(
            "sess-1", "t1", 4, gelesen=None, urteil="Richtig.", text="x")
    assert [a["arguments"]["action"] for a in fake.aufrufe("penecho_edit_canvas")] == ["create_text"]


def test_bleibt_der_platz_belegt_gibt_es_nur_einen_zweiten_versuch(zustand: Path) -> None:
    fake = FeldFake()
    r = rueckmeldungsfeld(4)
    alt = {"id": "text-box-7", "x": r["x"], "y": r["y"], "w": 380, "h": 120}
    with pytest.raises(PenEchoPlatzBelegt):
        PenEchoMcp(zustand, transport=_belegt_durch(fake, [alt], wie_oft=5)).rueckmeldung_zeigen(
            "sess-1", "t1", 4, gelesen=None, urteil="Richtig.", text="x")
    assert [a["arguments"]["action"] for a in fake.aufrufe("penecho_edit_canvas")] == ["create_text", "delete", "create_text"]


def test_rueckmeldung_setzen_fuer_getippte_antworten(zustand: Path) -> None:
    fake = Fake()
    _client(zustand, fake).rueckmeldung_setzen("sess-1", "t1", 2, "Rückmeldung zu Aufgabe 2", "Richtig – gut gemacht.")
    arg = fake.aufrufe("penecho_edit_canvas")[0]["arguments"]
    assert arg["region"] == rueckmeldungsfeld(2)
    assert arg["text"] == "Rückmeldung zu Aufgabe 2\n\nRichtig – gut gemacht."


def test_rueckmeldung_unbekanntes_urteil_wirft_ohne_penecho(zustand: Path) -> None:
    fake = Fake()
    with pytest.raises(ValueError):
        _client(zustand, fake).rueckmeldung_zeigen("s", "t", 1, gelesen=None, urteil="Super!", text="x")
    assert fake.anfragen == []


def test_bericht_zeigt_karte_und_schnappschuesse_mit_festen_kennungen(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    c.bericht_zeigen("sess-1", LERN_SID, "# Bericht: K", (1, 2))
    c.bericht_zeigen("sess-1", LERN_SID, "# Bericht: K", (1, 2))
    karte = fake.aufrufe("penecho_present_widget")
    assert [k["arguments"]["artifactId"] for k in karte] == [f"bericht-{LERN_SID}"] * 2
    assert karte[0]["arguments"] == karte[1]["arguments"]
    bilder = fake.aufrufe("penecho_place_image")
    assert [b["arguments"]["requestId"] for b in bilder] == [f"bericht-{LERN_SID}-{n}" for n in (1, 2, 1, 2)]
    assert bilder[0]["arguments"]["source"].startswith("data:image/png;base64,")
    assert len(fake.aufrufe("penecho_capture_canvas")) == 4


def test_bericht_ohne_nummern_nur_karte(zustand: Path) -> None:
    fake = Fake()
    _client(zustand, fake).bericht_zeigen("sess-1", LERN_SID, "x")
    assert len(fake.aufrufe("penecho_present_widget")) == 1 and fake.aufrufe("penecho_place_image") == []


def test_bericht_ein_bild_scheitert_karte_bleibt(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    c.schnappschuss = lambda *a, **k: bytes.fromhex("89504e470d0a1a0a") + b"0" * 900_000  # type: ignore[method-assign]
    c.bericht_zeigen("sess-1", LERN_SID, "x", (1,))
    assert len(fake.aufrufe("penecho_present_widget")) == 1 and fake.aufrufe("penecho_place_image") == []


def test_bericht_transportfehler_beendet_bildschleife(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    orig = c._werkzeug

    def werkzeug(name, argumente):
        if name == "penecho_place_image":
            raise PenEchoNichtErreichbar("jsonrpc")
        return orig(name, argumente)

    c._werkzeug = werkzeug  # type: ignore[method-assign]
    c.bericht_zeigen("sess-1", LERN_SID, "x", (1, 2, 3))
    assert len(fake.aufrufe("penecho_capture_canvas")) == 1


def test_bericht_konflikt_ueberspringt_nur_dieses_bild(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    orig = c._werkzeug
    erstes = []

    def werkzeug(name, argumente):
        if name == "penecho_place_image" and not erstes:
            erstes.append(1)
            raise PenEchoAnfrageSchonBekannt("REQUEST_ID_CONFLICT")
        return orig(name, argumente)

    c._werkzeug = werkzeug  # type: ignore[method-assign]
    c.bericht_zeigen("sess-1", LERN_SID, "x", (1, 2))
    assert len(fake.aufrufe("penecho_place_image")) == 1


def test_bericht_zeitbudget_ueberspringt_restliche_bilder(zustand: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import spaces.learning.huelle.penecho_mcp as modul

    zeit = iter([0.0, 1.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(modul, "_uhr", lambda: next(zeit))
    fake = Fake()
    _client(zustand, fake).bericht_zeigen("sess-1", LERN_SID, "x", (1, 2, 3))
    assert len(fake.aufrufe("penecho_place_image")) == 1


def test_bericht_kartentitel_auf_120_zeichen_gekuerzt(zustand: Path) -> None:
    fake = Fake()
    _client(zustand, fake).bericht_zeigen("sess-1", LERN_SID, "# Bericht: " + "K" * 300 + "\n")
    titel = fake.aufrufe("penecho_present_widget")[0]["arguments"]["title"]
    assert len(titel) == 120 and titel.endswith("\u2026")


def test_rueckmeldung_ist_kein_widget_der_bericht_bleibt_karte(zustand: Path) -> None:
    # Abnahme 06.11.: PenEcho setzte Rueckmeldungs-Widgets selbst und belegte die naechsten Aufgabenbereiche.
    fake = Fake()
    c = _client(zustand, fake)
    c.rueckmeldung_zeigen("sess-1", "t1", 4, gelesen="B", urteil="Richtig.", text="Gut.")
    c.bericht_zeigen("sess-1", "s1", "# Bericht\nText")
    (bericht,) = [a["arguments"] for a in fake.aufrufe("penecho_present_widget")]
    assert bericht["artifactId"] == "bericht-s1" and "presentation" not in bericht


def test_zeigen_kommt_vor_dem_rahmen(zustand: Path) -> None:
    # Abnahme 06.11.: auf einem frisch angelegten Board lehnte PenEcho draw_ink mit ACTIVE_CANVAS_REQUIRED ab.
    fake = FeldFake()
    _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "x", 2, mit_ergebnisfeld=True)
    aktionen = [a["arguments"]["action"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert aktionen.index("show") < aktionen.index("draw_ink")


def test_unsichtbares_board_wird_gezeigt_und_der_rahmen_neu_gezeichnet(zustand: Path) -> None:
    fake = FeldFake(revision=3)
    PenEchoMcp(zustand, transport=_fehler_bei_ink(fake, "ACTIVE_CANVAS_REQUIRED")).aufgabe_setzen(
        "s", "t", "x", 1, mit_ergebnisfeld=True)
    aktionen = [a["arguments"]["action"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert aktionen[-3:] == ["draw_ink", "show", "draw_ink"]
    assert fake.gezeichnet == 1


# ---- Abnahme 06.11.: Arbeitsfeld fuer offene und Zeichenaufgaben -----------------------------

@pytest.mark.parametrize("nummer", range(1, 21))
def test_arbeitsfeld_rechts_neben_dem_text_mit_rahmen_im_bereich(nummer: int) -> None:
    # Ohne sichtbare Grenze ragte eine Skizze in den Bereich darueber und an den Rueckmeldungsplatz.
    b, a, t = bereich(nummer), arbeitsfeld(nummer), textfeld(nummer)
    assert a["w"] >= 700 and a["h"] >= 600
    assert _innen({"x": a["x"] - 16, "y": a["y"] - 60, "w": a["w"] + 32, "h": a["h"] + 76}, b)
    assert a["x"] - 16 >= t["x"] + t["w"]


def test_aufgabe_mit_arbeitsfeld_zeichnet_rahmen_hinweis_im_aufgabentext(zustand: Path) -> None:
    fake = FeldFake(revision=4)
    _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "Skizzieren", 8, mit_arbeitsfeld=True)
    edits = [e["arguments"] for e in fake.aufrufe("penecho_edit_canvas")]
    (text,) = [e for e in edits if e["action"] == "create_text"]
    assert text["text"] == "Aufgabe 8: Skizzieren\n\nIhre Lösung schreiben Sie in das blau umrandete Feld."
    (rahmen,) = _ink(fake)
    assert rahmen["requestId"] == "arbeit-t1-8" and rahmen["baseRevision"] == 4
    a = arbeitsfeld(8)
    punkte = [q for s in rahmen["strokes"] for q in s["points"]]
    assert min(q["x"] for q in punkte) < a["x"] and max(q["x"] for q in punkte) > a["x"] + a["w"]
    assert min(q["y"] for q in punkte) < a["y"] and max(q["y"] for q in punkte) > a["y"] + a["h"]
    assert fake.aufrufe("penecho_capture_canvas") == []


def test_aufgabe_schon_auf_dem_board_wird_nicht_doppelt_gesetzt(zustand: Path) -> None:
    # Abnahme 06.11.: nach Neustart (neue PenEcho-Sitzung, feste requestId unbekannt) stand der eigene
    # Aufgabentext an der Textfeld-Ecke; die Huelle wich aus und setzte ihn ein zweites Mal.
    fake = Fake()
    echt = fake.__call__
    t = textfeld(8)

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        args = body.get("params", {}).get("arguments", {}) if body.get("method") == "tools/call" else {}
        if args.get("action") == "create_text" and "region" in args:
            fake.anfragen.append((body, request.headers))
            sc = {"code": "LAYOUT_CONFLICT", "message": "m", "details": {"obstacles": [
                {"id": "text-box-3", "x": t["x"], "y": t["y"], "w": 400, "h": 300}]}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": True, "structuredContent": sc}})
        return echt(request)

    PenEchoMcp(zustand, transport=httpx.MockTransport(antwort)).aufgabe_setzen("sess-1", "t1", "Skizzieren", 8)
    edits = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert [e["action"] for e in edits] == ["create_text", "show"]
    assert edits[1]["objectId"] == "text-box-3"


# ---- Abnahme 06.11.: Aufgabenbilder auf der Flaeche ------------------------------------------

BILD_PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 32


def _getrennt(a: dict, b: dict) -> bool:
    return (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
            or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


@pytest.mark.parametrize("nummer", range(1, 21))
def test_bildfeld_unter_dem_ergebnisfeld_im_bereich(nummer: int) -> None:
    f, e, t = bildfeld(nummer, unter_ergebnisfeld=True), ergebnisfeld(nummer), textfeld(nummer)
    assert _innen(f, bereich(nummer))
    assert _getrennt(f, {"x": e["x"] - 16, "y": e["y"] - 60, "w": e["w"] + 32, "h": e["h"] + 76})  # Rahmen + Schild
    assert _getrennt(f, t)
    assert f["w"] >= 700 and f["h"] >= 400


@pytest.mark.parametrize("nummer", range(1, 21))
def test_bildfeld_oben_im_arbeitsfeld(nummer: int) -> None:
    # Offene Aufgaben: das Bild steht im Arbeitsfeld, darunter bleibt Platz zum Schreiben und Beschriften.
    f, a = bildfeld(nummer, unter_ergebnisfeld=False), arbeitsfeld(nummer)
    assert _innen(f, a)
    assert f["h"] <= a["h"] // 2 and f["w"] >= 700


def _bilder(fake: Fake) -> list[dict]:
    return [a["arguments"] for a in fake.aufrufe("penecho_place_image")]


def test_aufgabenbilder_im_raster_mit_beschriftung(zustand: Path) -> None:
    fake = FeldFake(revision=4)
    bilder = [Aufgabenbild(f"Graph {c}", BILD_PNG, 480 / 360) for c in "ABCD"]
    _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "Ordnen Sie zu", 10, mit_ergebnisfeld=True, bilder=bilder)
    gesetzt = _bilder(fake)
    assert [b["requestId"] for b in gesetzt] == [f"bild-t1-10-{i}" for i in range(1, 5)]
    assert all(b["source"] == "data:image/png;base64," + base64.b64encode(BILD_PNG).decode() for b in gesetzt)
    feld = bildfeld(10, unter_ergebnisfeld=True)
    regionen = [b["region"] for b in gesetzt]
    assert all(_innen(r, feld) for r in regionen)
    assert all(_getrennt(a, b) for i, a in enumerate(regionen) for b in regionen[i + 1:])
    assert all(b["width"] == r["w"] and r["w"] >= 240 for b, r in zip(gesetzt, regionen))
    assert all(abs(r["w"] / r["h"] - 480 / 360) < 0.02 for r in regionen)
    schilder = [e for e in (a["arguments"] for a in fake.aufrufe("penecho_edit_canvas"))
                if e["action"] == "create_text" and e["requestId"].startswith("bild-")]
    assert [s["text"] for s in schilder] == ["Graph A", "Graph B", "Graph C", "Graph D"]
    for s, r in zip(schilder, regionen):
        assert s["requestId"].endswith("-schild") and _innen(s["region"], feld)
        assert s["region"]["y"] + s["region"]["h"] <= r["y"] and _getrennt(s["region"], r)
    # Bilder vor dem Rahmen: der Rahmen ist Tinte und wuerde sonst als Hindernis zaehlen.
    namen = [(b["params"]["name"], b["params"]["arguments"].get("action")) for b, _ in fake.anfragen
             if b["method"] == "tools/call"]
    assert namen.index(("penecho_place_image", None)) < namen.index(("penecho_edit_canvas", "draw_ink"))


def test_einzelbild_ohne_beschriftung_bei_offener_aufgabe(zustand: Path) -> None:
    fake = FeldFake(revision=4)
    _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "Beschriften Sie", 3, mit_arbeitsfeld=True,
                                          bilder=[Aufgabenbild(None, BILD_PNG, 2.0)])
    (b,) = _bilder(fake)
    assert _innen(b["region"], bildfeld(3, unter_ergebnisfeld=False))
    assert not [e for e in (a["arguments"] for a in fake.aufrufe("penecho_edit_canvas"))
                if e["requestId"].startswith("bild-") and e["action"] == "create_text"]


def test_belegter_bildplatz_wird_nicht_doppelt_gesetzt(zustand: Path) -> None:
    # Nach einem Neustart steht das Bild schon da (neue PenEcho-Sitzung kennt die requestId nicht).
    fake = Fake()
    echt = fake.__call__

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        params = body.get("params", {}) if body.get("method") == "tools/call" else {}
        args = params.get("arguments", {})
        if params.get("name") == "penecho_place_image" or str(args.get("requestId", "")).startswith("bild-"):
            fake.anfragen.append((body, request.headers))
            sc = {"code": "LAYOUT_CONFLICT", "message": "m", "details": {"obstacles": [
                {"id": "image-1", "x": 0, "y": 0, "w": 10, "h": 10}]}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": True, "structuredContent": sc}})
        return echt(request)

    PenEchoMcp(zustand, transport=httpx.MockTransport(antwort)).aufgabe_setzen(
        "sess-1", "t1", "Ordnen", 10, bilder=[Aufgabenbild("Graph A", BILD_PNG, 1.0)])
    gesetzt = _bilder(fake)
    assert len(gesetzt) == 1 and "region" in gesetzt[0]  # kein zweiter Versuch ohne Region


def test_bild_in_fremdem_format_wird_ausgelassen(zustand: Path, caplog: pytest.LogCaptureFixture) -> None:
    fake = Fake()
    with caplog.at_level(logging.WARNING):
        _client(zustand, fake).aufgabe_setzen("sess-1", "t1", "Ordnen", 10,
                                              bilder=[Aufgabenbild("A", b"GIF89a" + b"0" * 10, 1.0),
                                                      Aufgabenbild("B", BILD_PNG, 1.0)])
    assert [b["requestId"] for b in _bilder(fake)] == ["bild-t1-10-2"]
    assert "Aufgabenbild" in caplog.text


_LANG = 'Satz 0: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 1: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 2: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 3: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 4: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 5: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 6: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 7: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 8: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 9: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 10: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 11: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 12: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 13: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 14: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 15: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 16: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 17: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 18: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 19: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 20: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 21: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 22: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 23: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 24: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 25: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 26: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 27: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 28: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 29: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 30: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 31: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 32: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 33: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 34: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 35: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 36: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 37: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 38: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 39: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet.'


def _zeilen_bedarf(text: str, je_zeile: int = 36) -> int:
    return sum(max(1, -(-len(z) // je_zeile)) for z in text.split("\n"))


def test_lange_rueckmeldung_wird_fuer_die_flaeche_gekuerzt(zustand: Path) -> None:
    # Abnahme 06.11. (F14): Rubrik-Rueckmeldung ~2400 Zeichen -> PenEcho INVALID_TEXT (max. 2000), und
    # selbst darunter ragte der Text ueber das Rueckmeldungsfeld in den naechsten Bereich.
    fake = Fake()
    _client(zustand, fake).rueckmeldung_zeigen("sess-1", "t1", 4, gelesen=None, urteil="Teilweise richtig.",
                                                text=_LANG)
    text = fake.aufrufe("penecho_edit_canvas")[0]["arguments"]["text"]
    assert len(text) <= 2000 and _zeilen_bedarf(text, 64) <= 28
    assert text.startswith("Rückmeldung zu Aufgabe 4\n\nTeilweise richtig.\n\nSatz 0:")
    assert text.endswith(" …") and not text.endswith(": …")


def test_normaler_bereich_fasst_eine_lange_rueckmeldung(zustand: Path) -> None:
    # A3 (offen aus A2, Befund 6): in normal hohen Bereichen brach die Karte nach rund 1000 Zeichen mit „…“
    # ab. Das Feld ist jetzt 760 breit, die Lernflaeche bricht an dieser Breite um (textUmbruchBreite).
    fake = Fake()
    lang = _LANG[:_LANG.rindex("begründet.", 0, 1500) + len("begründet.")]
    _client(zustand, fake).rueckmeldung_setzen("sess-1", "t1", 4, "Rückmeldung zu Aufgabe 4", lang)
    arg = fake.aufrufe("penecho_edit_canvas")[0]["arguments"]
    assert arg["region"]["w"] == 760 and arg["region"]["h"] == 760
    assert arg["text"] == f"Rückmeldung zu Aufgabe 4\n\n{lang}"


def test_rueckmeldung_nutzt_die_hoehe_eines_hohen_bereichs(zustand: Path) -> None:
    # Abnahme A2 (Befund 6): im Listenkurs sind Bereiche mit Bild hoeher; die Karte wurde trotzdem nach
    # 28 Zeilen (Hoehe des 800er-Rasters) mit „…“ abgeschnitten.
    fake = Fake()
    hoch = {"x": 100, "y": 100, "w": 1300, "h": 1600}
    lang = _LANG[:_LANG.rindex("begründet.", 0, 1500) + len("begründet.")]
    _client(zustand, fake).rueckmeldung_setzen("sess-1", "t1", 4, "Rückmeldung zu Aufgabe 4", lang, rechteck=hoch)
    text = fake.aufrufe("penecho_edit_canvas")[0]["arguments"]["text"]
    assert 28 < _zeilen_bedarf(text) <= (1600 - 40) // 27
    assert text == f"Rückmeldung zu Aufgabe 4\n\n{lang}"  # knapp 1500 Zeichen passen ganz in den hohen Bereich


def test_volle_flaeche_zeigt_aufgabe_trotzdem(zustand: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Abnahme 06.11. (F15): PenEcho prueft die Textgrenze vor dem Platz. Auf einer vollen Flaeche
    # scheiterte selbst das erneute Setzen einer vorhandenen Aufgabe mit OBJECT_LIMIT, und die Huelle
    # meldete dauerhaft "nicht verbunden". Der Aufgabentext entfaellt dann, Board und Rahmen bleiben.
    fake = FeldFake()
    echt = fake.__call__

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        args = body.get("params", {}).get("arguments", {}) if body.get("method") == "tools/call" else {}
        if args.get("action") == "create_text":
            fake.anfragen.append((body, request.headers))
            sc = {"code": "OBJECT_LIMIT", "message": "m", "details": {"kind": "text", "limit": 50}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "isError": True, "structuredContent": sc, "content": []}})
        return echt(request)

    with caplog.at_level(logging.WARNING):
        PenEchoMcp(zustand, transport=httpx.MockTransport(antwort)).aufgabe_setzen(
            "sess-1", "t1", "x", 4, mit_ergebnisfeld=True)
    aktionen = [a["arguments"]["action"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert aktionen[0] == "create_text" and "show" in aktionen and len(_ink(fake)) == 1
    assert "Aufgabentext nicht gesetzt: Flaeche voll" in caplog.text


def test_volle_flaeche_bei_rueckmeldung_ist_eigener_fehler(zustand: Path) -> None:
    fake = FeldFake()

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            sc = {"code": "OBJECT_LIMIT", "message": "m"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "isError": True, "structuredContent": sc, "content": []}})
        return fake(request)

    with pytest.raises(PenEchoFlaecheVoll):
        PenEchoMcp(zustand, transport=httpx.MockTransport(antwort)).rueckmeldung_zeigen(
            "sess-1", "t1", 4, gelesen=None, urteil="Richtig.", text="x")


def _antwort_mit_fehler(fake: Fake, trifft) -> httpx.MockTransport:
    """Transport: Werkzeugaufrufe, fuer die `trifft(name, args)` einen Code liefert, scheitern damit."""

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            code = trifft(body["params"]["name"], body["params"]["arguments"])
            if code:
                fake.anfragen.append((body, request.headers))
                sc = {"code": code, "message": "m"}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                    "isError": True, "structuredContent": sc, "content": []}})
        return fake(request)

    return httpx.MockTransport(antwort)


def _schilder(fake: Fake) -> list[dict]:
    return [e for e in (a["arguments"] for a in fake.aufrufe("penecho_edit_canvas"))
            if e["action"] == "create_text" and e["requestId"].startswith("bild-")]


def test_volle_flaeche_setzt_keine_bildschilder(zustand: Path) -> None:
    # Abnahme 06.11. (F17): Jeder gescheiterte Aenderungsaufruf bleibt in PenEchos Sitzung als
    # "unaufgeloest" stehen (bound-operations.js MAX_UNRESOLVED_PATCHES = 4). Auf der vollen Flaeche
    # scheiterten Aufgabentext und jede Bildbeschriftung mit OBJECT_LIMIT; danach nahm PenEcho
    # nichts mehr an (request_limit), Bild 3 und 4 und der Rahmen fehlten.
    fake = FeldFake()
    bilder = [Aufgabenbild(f"Graph {c}", BILD_PNG, 480 / 360) for c in "ABCD"]
    transport = _antwort_mit_fehler(
        fake, lambda name, args: "OBJECT_LIMIT" if args.get("action") == "create_text" else None)
    PenEchoMcp(zustand, transport=transport).aufgabe_setzen(
        "sess-1", "t1", "Ordnen", 10, mit_ergebnisfeld=True, bilder=bilder)
    assert _schilder(fake) == []
    assert len(_bilder(fake)) == 4 and len(_ink(fake)) == 1


def test_erstes_volles_bildschild_beendet_die_beschriftung(zustand: Path) -> None:
    fake = FeldFake()
    bilder = [Aufgabenbild(f"Graph {c}", BILD_PNG, 480 / 360) for c in "ABCD"]
    transport = _antwort_mit_fehler(fake, lambda name, args: "OBJECT_LIMIT" if args.get("action") == "create_text"
                                    and args["requestId"].startswith("bild-") else None)
    PenEchoMcp(zustand, transport=transport).aufgabe_setzen(
        "sess-1", "t1", "Ordnen", 10, mit_ergebnisfeld=True, bilder=bilder)
    assert [s["text"] for s in _schilder(fake)] == ["Graph A"]
    assert len(_bilder(fake)) == 4 and len(_ink(fake)) == 1


def test_anfragestau_schliesst_die_sitzung(zustand: Path) -> None:
    # F17: request_limit loest sich in derselben PenEcho-Sitzung nicht mehr auf. Die Huelle schliesst
    # sie (penecho_close_session behaelt den Inhalt) und meldet sie als unbekannt; die Bruecke legt
    # dann ueber denselben sessionKey eine frische Sitzung auf demselben Board an.
    fake = FeldFake()
    transport = _antwort_mit_fehler(
        fake, lambda name, args: "request_limit" if name == "penecho_place_image" else None)
    with pytest.raises(PenEchoSitzungUnbekannt):
        PenEchoMcp(zustand, transport=transport).aufgabe_setzen(
            "sess-1", "t1", "Ordnen", 10, bilder=[Aufgabenbild("Graph A", BILD_PNG, 1.0)])
    assert [a["arguments"] for a in fake.aufrufe("penecho_close_session")] == [{"sessionId": "sess-1"}]


def _mit_belegung(fake: Fake, objekte: list[tuple[str, str, dict]]) -> httpx.MockTransport:
    """Transport: penecho_list_files liefert `objekte` (Art, objectId, Platz) wie PenEchos virtuelle Dateien."""

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call" and body["params"]["name"] == "penecho_list_files":
            fake.anfragen.append((body, request.headers))
            eintraege = [{"objectId": oid, "kind": art, "bounds": platz, "path": f"objects/{oid}/geometry.json"}
                         for art, oid, platz in objekte]
            sc = {"documentId": "d", "entries": eintraege, "total": len(eintraege)}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "structuredContent": sc, "content": []}})
        return fake(request)

    return httpx.MockTransport(antwort)


def _graphen() -> tuple[list[Aufgabenbild], list[tuple[dict | None, dict]]]:
    bilder = [Aufgabenbild(f"Graph {c}", BILD_PNG, 480 / 360) for c in "ABCD"]
    return bilder, bildraster(bildfeld(20, unter_ergebnisfeld=True), bilder)


def test_vorhandene_bilder_werden_gelesen_nicht_geprobt(zustand: Path) -> None:
    # Abnahme 06.11. (F18): Auf einem wiederhergestellten Board erkannte die Huelle "Bild steht schon da"
    # an LAYOUT_CONFLICT. Jeder solche Fehlversuch bleibt in PenEchos Sitzung unaufgeloest; bei vier
    # Bildern lief danach jeder Rahmen in request_limit, auch in einer frischen Sitzung. Jetzt liest die
    # Huelle die Belegung (penecho_list_files, nur lesend) und setzt nur, was fehlt.
    fake = FeldFake()
    bilder, plaetze = _graphen()
    objekte = [("text", "text-box-76", {**textfeld(20), "w": 393, "h": 518})]
    objekte += [("image", f"image-{i}", platz) for i, (_, platz) in enumerate(plaetze)]
    PenEchoMcp(zustand, transport=_mit_belegung(fake, objekte)).aufgabe_setzen(
        "sess-1", "t1", "Ordnen", 20, mit_ergebnisfeld=True, bilder=bilder)
    assert _bilder(fake) == []
    texte = [e for e in (a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")) if e["action"] == "create_text"]
    assert [t["text"] for t in texte] == ["Graph A", "Graph B", "Graph C", "Graph D"]  # fehlende Schilder ja
    assert len(_ink(fake)) == 1
    zeigen = [e for e in (a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")) if e["action"] == "show"]
    assert zeigen[0]["objectId"] == "text-box-76"


def test_vorhandene_bildschilder_werden_nicht_doppelt_gesetzt(zustand: Path) -> None:
    fake = FeldFake()
    bilder, plaetze = _graphen()
    objekte = [("image", f"image-{i}", platz) for i, (_, platz) in enumerate(plaetze)]
    objekte += [("text", f"text-box-{i}", schild) for i, (schild, _) in enumerate(plaetze) if schild]
    PenEchoMcp(zustand, transport=_mit_belegung(fake, objekte)).aufgabe_setzen(
        "sess-1", "t1", "Ordnen", 20, mit_ergebnisfeld=True, bilder=bilder)
    assert _bilder(fake) == [] and _schilder(fake) == []
    assert len(_ink(fake)) == 1


def test_belegung_nicht_lesbar_setzt_wie_bisher(zustand: Path) -> None:
    fake = FeldFake()
    bilder, _ = _graphen()
    transport = _antwort_mit_fehler(fake, lambda name, args: "invalid_arguments" if name == "penecho_list_files" else None)
    PenEchoMcp(zustand, transport=transport).aufgabe_setzen(
        "sess-1", "t1", "Ordnen", 20, mit_ergebnisfeld=True, bilder=bilder)
    assert len(_bilder(fake)) == 4 and len(_schilder(fake)) == 4 and len(_ink(fake)) == 1


def test_berichtsbilder_stehen_mit_fester_region_neben_ihrer_aufgabe(zustand: Path) -> None:
    # Abnahme 06.11. (F19): Ohne region rechnet PenEcho die Bildbreite durch den Zoom der Ansicht
    # (canvas-documents.js: w = width / viewport.scale). Bei 4 % wurden aus 900 dann 22500 > 20000,
    # INVALID_GEOMETRY, und kein Berichtsbild kam an. Mit region gilt Massstab 1.
    fake = Fake()
    _client(zustand, fake).bericht_zeigen("sess-1", LERN_SID, "# Bericht: K", (1, 20))
    bilder = [b["arguments"] for b in fake.aufrufe("penecho_place_image")]
    assert [b["region"] for b in bilder] == [berichtsplatz(1), berichtsplatz(20)]
    assert all(b["width"] == 700 for b in bilder)
    for n in (1, 20):
        p, r, b = berichtsplatz(n), rueckmeldungsfeld(n), bereich(n)
        assert p["x"] > r["x"] + r["w"] and p["y"] == b["y"]
        assert p["w"] == 700 and p["h"] <= b["h"] and p["x"] + p["w"] <= FLAECHE_GROESSE


def test_vorhandenes_berichtsbild_wird_nicht_erneut_gesetzt(zustand: Path) -> None:
    fake = Fake()
    objekte = [("image", "image-7", {**berichtsplatz(1), "h": 554}),
               ("widget", "widget-1", {"x": berichtsplatz(2)["x"] + 100, "y": berichtsplatz(2)["y"], "w": 300, "h": 300})]
    PenEchoMcp(zustand, transport=_mit_belegung(fake, objekte)).bericht_zeigen("sess-1", LERN_SID, "x", (1, 2, 3))
    assert [b["arguments"]["region"] for b in fake.aufrufe("penecho_place_image")] == [berichtsplatz(3)]
    assert len(fake.aufrufe("penecho_capture_canvas")) == 1  # nur fuer das Bild, das gesetzt wird


# ---- Teil A2, Task 4: Felder je Rechteck, Markenzeile, Bereich zeigen ------------------------------

def test_felder_relativ_zum_rechteck() -> None:
    assert felder(bereich(3)) == (textfeld(3), ergebnisfeld(3), arbeitsfeld(3), rueckmeldungsfeld(3), berichtsplatz(3))
    b = bereich(3)
    verschoben = {**b, "x": b["x"] + 2900, "y": b["y"] + 50}
    for alt, neu in zip(felder(b), felder(verschoben), strict=True):
        assert neu == {**alt, "x": alt["x"] + 2900, "y": alt["y"] + 50}


def test_felder_eines_grossen_bereichs_wachsen_nach_unten() -> None:
    klein, gross = anordnung([False])[0], anordnung([True])[0]
    k, g = felder(klein), felder(gross)
    assert g.ergebnisfeld == k.ergebnisfeld and g.textfeld["h"] == k.textfeld["h"] + 300
    assert g.arbeitsfeld["h"] == k.arbeitsfeld["h"] + 300 and g.berichtsplatz["h"] == gross["h"]
    assert bildfeld(1, unter_ergebnisfeld=True, rechteck=gross)["h"] == bildfeld(1, unter_ergebnisfeld=True)["h"] + 300


def test_rechteck_ueber_zwanzig(zustand: Path) -> None:
    fake = FeldFake()
    r = anordnung([False] * 25)[24]
    assert r["x"] == 3000
    _client(zustand, fake).aufgabe_setzen("sess-1", "t25", "Rechne", 25, mit_ergebnisfeld=True, rechteck=r)
    edits = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    (text,) = [e for e in edits if e["action"] == "create_text"]
    assert text["region"] == felder(r).textfeld and text["region"]["x"] == 3020
    assert text["text"].startswith("Aufgabe 25: Rechne") and text["requestId"] == "aufgabe-t25-25"
    assert [a["arguments"]["region"] for a in fake.aufrufe("penecho_list_files")] == [r]
    (ink,) = _ink(fake)
    e = felder(r).ergebnisfeld
    xs = [p["x"] for p in ink["strokes"][0]["points"]]
    assert min(xs) == e["x"] - 8 and max(xs) == e["x"] + e["w"] + 8
    # Mit Rechteck holt die Huelle den Bereich selbst ins Bild (bereich_zeigen); das Setzen verschiebt
    # die Ansicht nicht, sondern macht das Board nur sichtbar (Rahmen braucht ein aktives Board).
    (zeigen,) = [e for e in edits if e["action"] == "show"]
    assert "objectId" not in zeigen


def test_rueckmeldung_und_schnappschuss_mit_rechteck(zustand: Path) -> None:
    fake = Fake()
    c = _client(zustand, fake)
    r = anordnung([False] * 30)[29]
    c.rueckmeldung_zeigen("sess-1", "t30", 30, gelesen="4", urteil="Richtig.", text="Gut gemacht.", rechteck=r)
    (karte,) = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert karte["region"] == felder(r).rueckmeldungsfeld
    assert karte["text"].startswith("Rückmeldung zu Aufgabe 30")
    c.ergebnis_schnappschuss("sess-1", 30, rechteck=r)
    c.schnappschuss("sess-1", 30, rechteck=r)
    assert [a["arguments"]["region"] for a in fake.aufrufe("penecho_capture_canvas")] == [felder(r).ergebnisfeld, r]


def test_bericht_mit_rechtecken_je_nummer(zustand: Path) -> None:
    fake = Fake()
    bereiche = anordnung([False] * 25)
    _client(zustand, fake).bericht_zeigen("sess-1", LERN_SID, "# Bericht", (2, 25),
                                          rechteck={n: bereiche[n - 1] for n in (2, 25)})
    assert [b["arguments"]["region"] for b in fake.aufrufe("penecho_place_image")] == [
        felder(bereiche[1]).berichtsplatz, felder(bereiche[24]).berichtsplatz]
    assert [a["arguments"]["region"] for a in fake.aufrufe("penecho_capture_canvas")] == [bereiche[1], bereiche[24]]


class KontextFake(Fake):
    """Wie Fake, mit einer context.md wie PenEchos virtuelle Datei (Inhalt und contentHash)."""

    def __init__(self, inhalt: str, *, konflikt: int = 0) -> None:
        super().__init__()
        self.inhalt = inhalt
        self.konflikt = konflikt

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "tools/call":
            name, args = body["params"]["name"], body["params"]["arguments"]
            if name == "penecho_read_file" and args.get("path") == "context.md":
                self.anfragen.append((body, request.headers))
                return _ok(body, {"content": self.inhalt, "contentHash": f"h-{len(self.anfragen)}"})
            if name == "penecho_patch_file" and self.konflikt > 0:
                self.konflikt -= 1
                self.anfragen.append((body, request.headers))
                return _fehler(body, "SOURCE_CONFLICT")
        return super().__call__(request)


_KOPF = "--- a/context.md\n+++ b/context.md\n"


def _patches(fake: Fake) -> list[dict]:
    return [a["arguments"] for a in fake.aufrufe("penecho_patch_file")]


def test_bereiche_melden_ersetzt_marke(zustand: Path) -> None:
    alt = "<!-- lernflaeche-bereiche: [[100,100,1760,800]] -->"
    fake = KontextFake(f"# Kurs\nText davor\n{alt}\nText danach\n")
    bereiche = anordnung([False, True])
    _client(zustand, fake).bereiche_melden("sess-1", bereiche)
    (lesen,) = [a["arguments"] for a in fake.aufrufe("penecho_read_file")]
    assert lesen == {"sessionId": "sess-1", "path": "context.md"}
    (patch,) = _patches(fake)
    assert set(patch) == {"sessionId", "path", "contentHash", "patch", "requestId"}
    assert patch["path"] == "context.md" and patch["contentHash"] == "h-3" and patch["sessionId"] == "sess-1"
    assert patch["patch"] == f"{_KOPF}@@ -3,1 +3,1 @@\n-{alt}\n+{markenzeile(bereiche)}\n"
    assert 1 <= len(patch["requestId"]) <= 128


def test_bereiche_melden_haengt_an(zustand: Path) -> None:
    fake = KontextFake("# Kurs\nText\n")
    bereiche = anordnung([False] * 3)
    _client(zustand, fake).bereiche_melden("sess-1", bereiche)
    (patch,) = _patches(fake)
    assert patch["patch"] == f"{_KOPF}@@ -2,1 +2,2 @@\n Text\n+{markenzeile(bereiche)}\n"


def test_bereiche_melden_ohne_zeilenende_und_leer(zustand: Path) -> None:
    neu = markenzeile(anordnung([False]))
    fake = KontextFake("Text")
    _client(zustand, fake).bereiche_melden("sess-1", anordnung([False]))
    assert _patches(fake)[0]["patch"] == f"{_KOPF}@@ -1,1 +1,2 @@\n-Text\n\\ No newline at end of file\n+Text\n+{neu}\n"
    leer = KontextFake("")
    _client(zustand, leer).bereiche_melden("sess-1", anordnung([False]))
    assert _patches(leer)[0]["patch"] == f"{_KOPF}@@ -0,0 +1,1 @@\n+{neu}\n"


def test_bereiche_melden_marke_schon_aktuell_kein_patch(zustand: Path) -> None:
    bereiche = anordnung([False] * 2)
    fake = KontextFake(f"Text\n{markenzeile(bereiche)}\n")
    _client(zustand, fake).bereiche_melden("sess-1", bereiche)
    assert _patches(fake) == []


def test_bereiche_melden_liest_nach_konflikt_einmal_neu(zustand: Path) -> None:
    fake = KontextFake("Text\n", konflikt=1)
    _client(zustand, fake).bereiche_melden("sess-1", anordnung([False]))
    erster, zweiter = _patches(fake)
    assert erster["contentHash"] != zweiter["contentHash"] and erster["requestId"] != zweiter["requestId"]
    assert len(fake.aufrufe("penecho_read_file")) == 2


def test_bereich_zeigen_holt_die_zeile_ins_bild(zustand: Path) -> None:
    # Die Lernflaeche nimmt bei show einen Bereich (schema.js EDIT_FIELDS.show, seit 09.11.). Live rahmte die
    # Flaeche mit objectId nur den Aufgabentext, das Antwortfeld lag halb ausserhalb; jetzt die ganze Zeile.
    fake = Fake()
    r = anordnung([False] * 3)[2]
    objekte = [("text", "text-box-9", {**felder(r).textfeld, "h": 300})]
    PenEchoMcp(zustand, transport=_mit_belegung(fake, objekte)).bereich_zeigen("sess-1", r)
    assert fake.aufrufe("penecho_list_files") == []
    (zeigen,) = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert zeigen["action"] == "show" and zeigen["region"] == zeile(r)
    assert set(zeigen) == {"sessionId", "requestId", "action", "region"}


def test_setzen_ohne_zeigen_holt_kein_board_nach_vorn(zustand: Path) -> None:
    # Review 4dec9b7: show (auch ohne objectId) macht das Board in der Lernflaeche zum aktiven
    # (canvas-documents.js canvasDocumentsShow). Im Hintergrund darf das nicht passieren.
    r = anordnung([False] * 3)[2]
    fake = FeldFake()
    _client(zustand, fake).aufgabe_setzen("sess-1", "t3", "Rechne", 3, mit_ergebnisfeld=True, rechteck=r, zeigen=False)
    edits = [a["arguments"]["action"] for a in fake.aufrufe("penecho_edit_canvas")]
    assert edits == ["create_text", "draw_ink"]
    # Ist das Board gerade nicht aktiv (ACTIVE_CANVAS_REQUIRED), entfaellt der Rahmen, statt es zu zeigen.
    unsichtbar = FeldFake()
    PenEchoMcp(zustand, transport=_fehler_bei_ink(unsichtbar, "ACTIVE_CANVAS_REQUIRED", wie_oft=5)).aufgabe_setzen(
        "sess-1", "t3", "Rechne", 3, mit_ergebnisfeld=True, rechteck=r, zeigen=False)
    edits = [a["arguments"]["action"] for a in unsichtbar.aufrufe("penecho_edit_canvas")]
    assert edits == ["create_text", "draw_ink"]


# ---- Teil B, Task 6: eigene Flaeche je Person und Tab (canvasId) ---------------------------------------------

KA = "lf-" + "a" * 32
KB = "lf-" + "b" * 32


def _tab_fake(fake: Fake, *, konflikt: int = 0, verloren: bool = False):
    """start_session: sessionId und documentId je canvasId (ein Tab = eine Flaeche); mit documentId bleibt es dieses.

    `konflikt`: so oft session_key_conflict, wenn documentId mitkommt und der Schluessel der feste ist (ohne `#`;
    seine Bindung zeigt auf ein anderes Dokument). `verloren`: start_session mit documentId meldet STORAGE_UNAVAILABLE.
    """
    echt = fake.__call__
    rest = {"konflikt": konflikt}

    def antwort(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") != "tools/call" or body["params"]["name"] != "penecho_start_session":
            return echt(request)
        fake.anfragen.append((body, request.headers))
        args = body["params"]["arguments"]
        if args.get("documentId") and "#" not in args["sessionKey"] and rest["konflikt"] > 0:
            rest["konflikt"] -= 1
            return _fehler(body, "session_key_conflict")
        if args.get("documentId") and verloren:
            return _fehler(body, "STORAGE_UNAVAILABLE")
        tab = args.get("canvasId", "ohne")
        return _ok(body, {"sessionId": "pe-" + tab, "documentId": args.get("documentId") or "doc-" + tab})

    return antwort


def _starts(fake: Fake) -> list[dict]:
    return [a["arguments"] for a in fake.aufrufe("penecho_start_session")]


def test_sitzung_mit_flaeche_bindet_an_die_canvas_id(zustand: Path) -> None:
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake)))
    assert c.sitzung(LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    (start,) = _starts(fake)
    assert start["canvasId"] == KA and start["restore"] is True and start["title"] == TITEL
    assert "documentId" not in start  # noch kein Board gemerkt: PenEcho legt es in diesem Tab an
    assert start["sessionKey"].startswith("learning-huelle:") and len(start["sessionKey"]) <= 128
    assert start["sessionKey"] != "learning-huelle:" + LERN_SID  # eigener Schluessel je Tab
    assert set(start) <= _ERLAUBT["penecho_start_session"]
    # Derselbe Tab: keine neue Sitzung, auch nicht nach einem Neustart der Huelle.
    assert c.sitzung(LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    assert PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake))).sitzung(
        LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    assert len(_starts(fake)) == 1
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: "doc-" + KA}
    # Ohne Kennung (Telegram-Notweg, Hintergrund): die gebundene Sitzung bleibt, nichts Neues.
    assert c.sitzung(LERN_SID, TITEL) == "pe-" + KA
    assert len(_starts(fake)) == 1


def test_neuer_tab_bindet_dasselbe_board_per_document_id(zustand: Path) -> None:
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake)))
    c.sitzung(LERN_SID, TITEL, canvas_id=KA)
    assert c.sitzung(LERN_SID, TITEL, canvas_id=KB) == "pe-" + KB
    alt, neu = _starts(fake)
    # Der neue Tab bekommt genau das Board der Lernsitzung (documentId), unter eigenem Schluessel.
    assert neu["canvasId"] == KB and neu["documentId"] == "doc-" + KA
    assert neu["sessionKey"] != alt["sessionKey"]
    assert _merkdatei(zustand, "sitzungen.json") == {LERN_SID: "pe-" + KB}
    # Nie die Suche in anderen Browsern (das waeren die Tabs anderer Personen).
    assert fake.aufrufe("penecho_list_canvases") == [] and fake.aufrufe("penecho_open_canvas") == []
    # Zurueck in den ersten Tab: wieder dessen Schluessel, wieder mit dem Board.
    assert c.sitzung(LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    assert _starts(fake)[2]["sessionKey"] == alt["sessionKey"] and _starts(fake)[2]["documentId"] == "doc-" + KA


def test_alte_sitzung_ohne_tab_wird_beim_ersten_tab_gebunden(zustand: Path) -> None:
    """Vor Teil B gemerkte Sitzung (ohne canvasId): der erste Tab bindet das gemerkte Board an sich."""
    fake = Fake()
    alt = _client(zustand, fake)
    assert alt.sitzung(LERN_SID, TITEL) == "sess-1"
    _dokument_merken(zustand)
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake)))
    assert c.sitzung(LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    assert _starts(fake)[1]["canvasId"] == KA and _starts(fake)[1]["documentId"] == DOK


def test_schluessel_belegt_dann_frischer_schluessel_auf_derselben_flaeche(zustand: Path) -> None:
    _dokument_merken(zustand)
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake, konflikt=1)))
    assert c.sitzung(LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    erst, zweit = _starts(fake)
    assert zweit["canvasId"] == KA and zweit["documentId"] == DOK
    assert zweit["sessionKey"].startswith(erst["sessionKey"][:100]) and "#" in zweit["sessionKey"]
    assert len(zweit["sessionKey"]) <= 128


def test_verlorenes_board_im_tab_gibt_neues_board_in_diesem_tab(zustand: Path) -> None:
    _dokument_merken(zustand)
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake, verloren=True)))
    assert c.sitzung(LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    erst, zweit = _starts(fake)
    assert erst["documentId"] == DOK
    assert zweit["canvasId"] == KA and "documentId" not in zweit and zweit["sessionKey"] != erst["sessionKey"]
    assert fake.aufrufe("penecho_list_canvases") == [] and fake.aufrufe("penecho_open_canvas") == []
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: "doc-" + KA}


def test_ohne_canvas_id_genau_wie_bisher(zustand: Path) -> None:
    fake = Fake()
    assert _client(zustand, fake).sitzung(LERN_SID, TITEL, canvas_id=None) == "sess-1"
    assert _starts(fake) == [{"title": TITEL, "sessionKey": "learning-huelle:" + LERN_SID, "restore": True}]


# ---- Task 6, Fix-Runde 1 ---------------------------------------------------------------------------------------

def test_schluessel_belegt_und_dann_verloren_gibt_neues_board_mit_frischem_schluessel(zustand: Path) -> None:
    """Der Wiederholungsversuch nach session_key_conflict findet das Board nicht: neues Board, frischer Schluessel."""
    _dokument_merken(zustand)
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake, konflikt=1, verloren=True)))
    assert c.sitzung(LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    erst, zweit, dritt = _starts(fake)
    assert "#" not in erst["sessionKey"] and erst["documentId"] == DOK
    assert "#" in zweit["sessionKey"] and zweit["documentId"] == DOK
    assert dritt["canvasId"] == KA and "documentId" not in dritt and "#" in dritt["sessionKey"]
    assert fake.aufrufe("penecho_list_canvases") == [] and fake.aufrufe("penecho_open_canvas") == []
    assert _merkdatei(zustand, DOKUMENTE) == {LERN_SID: "doc-" + KA}


def test_frischer_schluessel_wird_gemerkt(zustand: Path) -> None:
    """Wie `ersatz` im alten Weg: nach `vergessen` (oder Neustart) gleich der frische Schluessel, kein neuer Konflikt."""
    _dokument_merken(zustand)
    fake = Fake()
    c = PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake, konflikt=5)))
    c.sitzung(LERN_SID, TITEL, canvas_id=KA)
    frisch = _starts(fake)[1]["sessionKey"]
    assert "#" in frisch
    c.vergessen(LERN_SID)
    neu = PenEchoMcp(zustand, transport=httpx.MockTransport(_tab_fake(fake, konflikt=5)))
    assert neu.sitzung(LERN_SID, TITEL, canvas_id=KA) == "pe-" + KA
    starts = _starts(fake)
    assert len(starts) == 3 and starts[2]["sessionKey"] == frisch and starts[2]["documentId"] == DOK
    # Der alte Weg (ohne Tab) behaelt seinen eigenen Ersatz-Schluessel; der Tab-Schluessel stoert ihn nicht.
    neu.vergessen(LERN_SID)
    alt = Fake()
    assert _client(zustand, alt).sitzung(LERN_SID, TITEL) == "sess-1"
    assert _starts(alt)[0]["sessionKey"] == "learning-huelle:" + LERN_SID


# ---- Teil C, Task 8: Namensschild im Mappenkopf ---------------------------------------------------------------

def test_schild_setzen_im_mappenkopf(zustand: Path) -> None:
    fake = Fake()
    kopf = {"x": 2420, "y": 120, "w": 1300, "h": 110}
    _client(zustand, fake).schild_setzen("sess-1", "mappe-7-p" + "a" * 24, "Anna A.", kopf)
    (arg,) = [a["arguments"] for a in fake.aufrufe("penecho_edit_canvas")]
    # Feste requestId: erneutes Setzen derselben Mappe wiederholt PenEcho nicht (Idempotenz).
    assert arg == {"sessionId": "sess-1", "requestId": "mappe-7-p" + "a" * 24, "action": "create_text",
                   "text": "Anna A.", "region": kopf}