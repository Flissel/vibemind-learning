"""KI-erzeugte Szene an der Aufgabe: das Werkzeug des Claw und der Abruf bei ComfyUI (nachgestellt)."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from spaces.learning.claw import server, szene, werkzeuge
from spaces.learning.claw.learning_client import LearningClient
from spaces.learning.tests.claw.test_claw_bild import API, KEY, MCP, TASK, Learning

BESCHREIBUNG = "Five colleagues in a meeting room, two of them looking at their phones"


def png(breite: int = 1024, hoehe: int = 768) -> bytes:
    puffer = io.BytesIO()
    Image.new("RGB", (breite, hoehe), (120, 140, 160)).save(puffer, "PNG")
    return puffer.getvalue()


class Comfy:
    """Nachgestelltes ComfyUI: Warteschlange, Auftrag, Verlauf und Bildabruf."""

    def __init__(self) -> None:
        self.anfragen: list[httpx.Request] = []
        self.laufend: list[Any] = []
        self.wartend: list[Any] = []
        self.polls_bis_fertig = 2
        self.fehler: dict[str, Any] | None = None
        self.bild = png()
        self.auftrag: dict[str, Any] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        pfad = request.url.path
        if pfad == "/queue":
            return httpx.Response(200, json={"queue_running": self.laufend, "queue_pending": self.wartend})
        if pfad == "/prompt":
            self.auftrag = json.loads(request.content)
            return httpx.Response(200, json={"prompt_id": self.auftrag["prompt_id"]})
        if pfad.startswith("/history/"):
            pid = pfad.rsplit("/", 1)[-1]
            self.polls_bis_fertig -= 1
            if self.polls_bis_fertig > 0:
                return httpx.Response(200, json={})
            if self.fehler is not None:
                return httpx.Response(200, json={pid: {"status": self.fehler, "outputs": {}}})
            return httpx.Response(200, json={pid: {"outputs": {"12": {"images": [
                {"filename": "szene_00001_.png", "subfolder": "", "type": "output"}]}}}})
        if pfad == "/view":
            return httpx.Response(200, content=self.bild)
        return httpx.Response(404)

    def pfade(self) -> list[str]:
        return [r.url.path for r in self.anfragen]


@pytest.fixture
def welt(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Comfy, Learning]]:
    c, lern = Comfy(), Learning()
    monkeypatch.setattr(werkzeuge, "_comfy_transport", lambda: httpx.MockTransport(c.handle))
    monkeypatch.setattr(werkzeuge, "_client", lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(lern.handle)))
    monkeypatch.setattr(szene, "_SCHLAF", lambda s: None)
    yield c, lern


# ---- Das Werkzeug ------------------------------------------------------------------------------------------

def test_setzen_erzeugt_legt_als_jpeg_ab_und_traegt_die_szene_ein(welt: tuple[Comfy, Learning]) -> None:
    c, lern = welt
    out = werkzeuge.werkstatt_szene_setzen(TASK, "Besprechung im Projektteam", BESCHREIBUNG)
    assert out["ok"] is True, out
    put = next(r for r in lern.requests if r.method == "PUT")
    assert put.content.startswith(b"\xff\xd8\xff") and put.headers["Content-Type"] == "image/jpeg"
    name = f"bild-{hashlib.sha256(put.content).hexdigest()[:16]}.jpg"
    assert put.url.path == f"/api/v1/lernsequenz-medien/{name}" and len(put.content) <= szene.MAX_BYTES
    body = json.loads(next(r for r in lern.requests if r.method == "PATCH").content)
    assert body["based_on"] == 4 and body["reason"] == "KI-erzeugtes Bild: Besprechung im Projektteam"
    assert body["felder"] == {"bild": {
        "image": f"/lernsequenz-medien/{name}", "width": 1024, "height": 768, "titel": "Besprechung im Projektteam",
        "herkunft": "ki", "beschreibung": BESCHREIBUNG, "modell": szene.MODELL,
    }}
    assert out["daten"] == {"fassung": 5, "bild": name, "quelle": "Bild: KI-erzeugt"}
    # Erst nachsehen, ob die Karte frei ist, dann genau ein Auftrag.
    assert c.pfade()[:2] == ["/queue", "/prompt"] and c.pfade().count("/prompt") == 1


def test_der_auftrag_traegt_beschreibung_rahmen_und_masse(welt: tuple[Comfy, Learning]) -> None:
    c, _ = welt
    assert werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG, format="hoch")["ok"] is True
    ablauf = c.auftrag["prompt"]
    text = ablauf["4"]["inputs"]["text"]
    assert text.startswith(BESCHREIBUNG) and "plain and unbranded" in text and "fictional" in text
    assert (ablauf["6"]["inputs"]["width"], ablauf["6"]["inputs"]["height"]) == (768, 1024)
    assert (ablauf["7"]["inputs"]["width"], ablauf["7"]["inputs"]["height"]) == (768, 1024)
    assert isinstance(ablauf["8"]["inputs"]["noise_seed"], int)
    assert ablauf["1"]["inputs"]["unet_name"].startswith("flux-2-klein")
    assert ablauf["12"]["inputs"]["filename_prefix"] == "lernaufgabe-szene"
    # Die Kennung des Auftrags vergibt der Abrufer selbst: so findet er ihn auch ohne Antwort wieder.
    assert len(c.auftrag["prompt_id"]) == 36


def test_besetzte_karte_wird_gemeldet_statt_einen_zweiten_auftrag_zu_stellen(welt: tuple[Comfy, Learning]) -> None:
    c, lern = welt
    for laufend, wartend in (([["x"]], []), ([], [["y"]])):
        c.anfragen.clear()
        c.laufend, c.wartend = laufend, wartend
        out = werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG)
        assert out["ok"] is False and "besetzt" in out["fehler"]
        assert c.pfade() == ["/queue"]
    assert not [r for r in lern.requests if r.method in ("PUT", "PATCH")]


def test_nicht_erreichbares_comfyui_wird_gemeldet(welt: tuple[Comfy, Learning], monkeypatch: pytest.MonkeyPatch) -> None:
    _, lern = welt

    def tot(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(werkzeuge, "_comfy_transport", lambda: httpx.MockTransport(tot))
    out = werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG)
    assert out == {"ok": False, "fehler": "Die Bild-KI (ComfyUI) ist auf diesem Rechner nicht erreichbar"}
    assert not [r for r in lern.requests if r.method in ("PUT", "PATCH")]


def test_fehler_der_bild_ki_wird_gemeldet(welt: tuple[Comfy, Learning]) -> None:
    c, lern = welt
    c.fehler = {"status_str": "error", "messages": [["execution_error", {
        "exception_type": "torch.OutOfMemoryError", "exception_message": "CUDA out of memory"}]]}
    out = werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG)
    assert out["ok"] is False and "Grafikspeicher" in out["fehler"]
    c.polls_bis_fertig = 2
    c.fehler = {"status_str": "error", "messages": []}
    assert "fehlgeschlagen" in werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG)["fehler"]
    assert not [r for r in lern.requests if r.method in ("PUT", "PATCH")]


def test_frist_wird_eingehalten(welt: tuple[Comfy, Learning], monkeypatch: pytest.MonkeyPatch) -> None:
    c, lern = welt
    c.polls_bis_fertig = 10_000
    uhr = iter(range(0, 100_000, 30))
    monkeypatch.setattr(szene, "_UHR", lambda: float(next(uhr)))
    out = werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG)
    assert out["ok"] is False and "zu lange" in out["fehler"]
    assert c.pfade().count("/prompt") == 1 and not [r for r in lern.requests if r.method in ("PUT", "PATCH")]


def test_auftrag_ohne_antwort_wird_nicht_neu_gestellt(welt: tuple[Comfy, Learning], monkeypatch: pytest.MonkeyPatch) -> None:
    # ComfyUI lädt beim Annehmen Modelle; bleibt die Antwort aus, läuft der Auftrag vielleicht trotzdem.
    c, _ = welt
    echt = c.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            c.anfragen.append(request)
            c.auftrag = json.loads(request.content)
            raise httpx.ReadTimeout("langsam")
        return echt(request)

    monkeypatch.setattr(werkzeuge, "_comfy_transport", lambda: httpx.MockTransport(handle))
    out = werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG)
    assert out["ok"] is True, out
    assert c.pfade().count("/prompt") == 1


@pytest.mark.parametrize("inhalt", [{"kind": "quiz", "prompt": "?"}, {"kind": "sketch", "prompt": "Zeichnen Sie."}])
def test_setzen_nur_an_offener_aufgabe_oder_fall(welt: tuple[Comfy, Learning], inhalt: dict[str, Any]) -> None:
    c, lern = welt
    lern.inhalt = inhalt
    out = werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG)
    assert out["ok"] is False and "offene Aufgaben und Fälle" in out["fehler"] and not c.anfragen


def test_setzen_prueft_titel_beschreibung_und_format(welt: tuple[Comfy, Learning]) -> None:
    c, _ = welt
    assert werkzeuge.werkstatt_szene_setzen(TASK, "", BESCHREIBUNG)["ok"] is False
    assert werkzeuge.werkstatt_szene_setzen(TASK, "T", "  ")["ok"] is False
    assert werkzeuge.werkstatt_szene_setzen(TASK, "T", "x" * 601)["ok"] is False
    assert werkzeuge.werkstatt_szene_setzen(TASK, "T", BESCHREIBUNG, format="riesig")["ok"] is False
    assert not c.anfragen


def test_aufgabe_aendern_setzt_keine_szene(welt: tuple[Comfy, Learning]) -> None:
    out = werkzeuge.werkstatt_aufgabe_aendern(TASK, 4, {"bild": {"herkunft": "ki"}}, "g")
    assert out["ok"] is False and "werkstatt_szene_setzen" in out["fehler"]


def test_werkzeug_ist_angemeldet() -> None:
    assert "werkstatt_szene_setzen" in {f.__name__ for f in server.WERKZEUGE}
    assert szene.URL_ENV in server._ENV_KEYS
    hier = Path(werkzeuge.__file__).parent
    assert "mcp__learning__werkstatt_szene_setzen" in (hier / "learning-mcp.json").read_text(encoding="utf-8")
    assert "werkstatt_szene_setzen" in (hier / "gateway" / "anbinden.sh").read_text(encoding="utf-8")
    assert "werkstatt_szene_setzen" in (hier / "AGENTS.md").read_text(encoding="utf-8")


# ---- Bausteine ---------------------------------------------------------------------------------------------

def test_formate_sind_durch_16_teilbar() -> None:
    assert szene.FORMATE == {"quer": (1024, 768), "hoch": (768, 1024), "quadrat": (896, 896)}
    assert all(b % 16 == 0 and h % 16 == 0 for b, h in szene.FORMATE.values())


def test_rahmen_haengt_die_festen_regeln_an() -> None:
    text = szene.rahmen("  A busy   office\nkitchen ")
    assert text.startswith("A busy office kitchen. ")
    for regel in ("plain and unbranded", "blank", "anonymous, fictional"):
        assert regel in text, regel
    # Echter Befund 20.11.: Auf „no logos“ zeichnete FLUX.2 ein Markenlogo. Der Rahmen sagt deshalb, was zu sehen
    # ist, und nennt das Unerwünschte gar nicht erst.
    for wort in ("logo", "brand name", "watermark", "celebrity", "no text"):
        assert wort not in text.lower(), wort


def test_als_jpeg_wandelt_und_haelt_die_grenze() -> None:
    daten, breite, hoehe = szene.als_jpeg(png(896, 896))
    assert daten.startswith(b"\xff\xd8\xff") and (breite, hoehe) == (896, 896) and len(daten) <= szene.MAX_BYTES
    for fremd in (b"", b"GIF89a" + b"x" * 40, b"\x89PNG\r\n\x1a\n" + b"kaputt"):
        with pytest.raises(szene.SzeneFehler):
            szene.als_jpeg(fremd)


def test_adresse_nur_auf_diesem_rechner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(szene.URL_ENV, raising=False)
    assert szene.adresse() == "http://127.0.0.1:8188"
    monkeypatch.setenv(szene.URL_ENV, "http://localhost:8190/")
    assert szene.adresse() == "http://localhost:8190"
    # Die Beschreibung einer Aufgabe geht nie an einen fremden Rechner.
    for fremd in ("https://example.org", "http://10.0.0.5:8188", "http://127.0.0.1.example.org:8188", "ftp://127.0.0.1"):
        monkeypatch.setenv(szene.URL_ENV, fremd)
        with pytest.raises(szene.SzeneFehler):
            szene.adresse()
