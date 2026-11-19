"""Diagramm an der Aufgabe: das Werkzeug des Claw, das Zeichnen mit Graphviz und die HTML-Datei mit SVG."""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from spaces.learning.claw import server, werkzeuge, zeichner
from spaces.learning.claw.learning_client import LearningClient
from spaces.learning.tests.claw.test_claw_bild import API, KEY, MCP, TASK, Learning

KNOTEN = [{"id": "pro", "text": "Prophase"}, {"id": "meta", "text": "Metaphase", "leer": True}]
KANTEN = [{"von": "pro", "nach": "meta", "text": "dann"}]
SVG = ('<?xml version="1.0"?>\n<!DOCTYPE svg PUBLIC "x" "y">\n<svg width="90pt" height="120pt" '
       'xmlns="http://www.w3.org/2000/svg"><text>Prophase</text></svg>\n')


def png(breite: int, hoehe: int, fuell: int = 50) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", breite, hoehe) + b"p" * fuell


class Zeichner:
    """Nachgestelltes Graphviz: merkt sich den Graphviz-Text und liefert PNG oder SVG."""

    def __init__(self) -> None:
        self.aufrufe: list[tuple[str, str]] = []
        self.png = png(320, 480)

    def __call__(self, dot: str, art: str) -> bytes:
        self.aufrufe.append((dot, art))
        return self.png if art == "png" else SVG.encode("utf-8")


@pytest.fixture
def welt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Zeichner, Learning, Path]]:
    z, lern = Zeichner(), Learning()
    monkeypatch.setattr(werkzeuge, "_zeichnen", lambda: z)
    monkeypatch.setattr(werkzeuge, "_client", lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(lern.handle)))
    monkeypatch.setenv(zeichner.ORDNER_ENV, str(tmp_path))
    yield z, lern, tmp_path


# ---- Das Werkzeug ------------------------------------------------------------------------------------------

def test_setzen_zeichnet_legt_ab_und_traegt_das_diagramm_ein(welt: tuple[Zeichner, Learning, Path]) -> None:
    z, lern, ordner = welt
    out = werkzeuge.werkstatt_diagramm_setzen(TASK, "Ablauf der Mitose", KNOTEN, KANTEN)
    assert out["ok"] is True, out
    name = f"bild-{hashlib.sha256(z.png).hexdigest()[:16]}.png"
    put = next(r for r in lern.requests if r.method == "PUT")
    assert put.url.path == f"/api/v1/lernsequenz-medien/{name}" and put.content == z.png
    assert put.headers["Content-Type"] == "image/png"
    body = json.loads(next(r for r in lern.requests if r.method == "PATCH").content)
    assert body["based_on"] == 4 and body["reason"] == "Diagramm: Ablauf der Mitose (2 Kästen, 1 Pfeile)"
    assert body["felder"] == {"bild": {
        "image": f"/lernsequenz-medien/{name}", "width": 320, "height": 480, "titel": "Ablauf der Mitose",
        "herkunft": "diagramm",
        "diagramm": {"richtung": "oben-unten",
                     "knoten": [{"id": "pro", "text": "Prophase", "leer": False},
                                {"id": "meta", "text": "Metaphase", "leer": True}],
                     "kanten": [{"von": "pro", "nach": "meta", "text": "dann"}], "gruppen": []},
    }}
    assert out["daten"]["fassung"] == 5 and out["daten"]["bild"] == name
    assert out["daten"]["quelle"] == "Diagramm: Ablauf der Mitose, eigene Darstellung"
    # Gezeichnet wird zweimal aus demselben Text: PNG für die Fläche, SVG für die HTML-Datei.
    assert [art for _dot, art in z.aufrufe] == ["png", "svg"] and z.aufrufe[0][0] == z.aufrufe[1][0]
    assert 'label="Prophase"' in z.aufrufe[0][0] and "Metaphase" not in z.aufrufe[0][0]
    html = Path(out["daten"]["html"])
    assert html.parent == ordner and html.suffix == ".html" and html.name.startswith("ablauf-der-mitose-")


def test_html_datei_ist_eine_einzelne_seite_ohne_fremde_dateien(welt: tuple[Zeichner, Learning, Path]) -> None:
    out = werkzeuge.werkstatt_diagramm_setzen(TASK, "Zelle & <Kern>", KNOTEN, KANTEN)
    seite = Path(out["daten"]["html"]).read_text(encoding="utf-8")
    assert seite.startswith("<!doctype html>") and '<meta charset="utf-8">' in seite
    assert "<title>Zelle &amp; &lt;Kern&gt;</title>" in seite and "<h1>Zelle &amp; &lt;Kern&gt;</h1>" in seite
    # Das SVG steht in der Seite; Vorspann und DOCTYPE von Graphviz nicht.
    assert "<svg " in seite and "<?xml" not in seite and "<!DOCTYPE svg" not in seite
    # Nichts wird nachgeladen und nichts ausgeführt: die Konzept-Fläche hat kein Netz.
    for fremd in ("<script", "http://", "https://", "<link", "<img", "@import", "url("):
        assert fremd not in seite.replace('xmlns="http://www.w3.org/2000/svg"', ""), fremd


def test_setzen_meldet_fehler_in_der_liste_und_schreibt_nichts(welt: tuple[Zeichner, Learning, Path]) -> None:
    z, lern, ordner = welt
    out = werkzeuge.werkstatt_diagramm_setzen(TASK, "X", KNOTEN, [{"von": "pro", "nach": "fehlt"}])
    assert out["ok"] is False and "fehlt" in out["fehler"]
    assert werkzeuge.werkstatt_diagramm_setzen(TASK, "", KNOTEN, KANTEN)["ok"] is False
    assert werkzeuge.werkstatt_diagramm_setzen(TASK, "X", [], [])["ok"] is False
    assert werkzeuge.werkstatt_diagramm_setzen(TASK, "X", KNOTEN, KANTEN, richtung="quer")["ok"] is False
    assert not z.aufrufe and not [r for r in lern.requests if r.method in ("PUT", "PATCH")] and not list(ordner.iterdir())


def test_setzen_nur_an_offener_aufgabe_fall_oder_skizze(welt: tuple[Zeichner, Learning, Path]) -> None:
    z, lern, _ = welt
    lern.inhalt = {"kind": "quiz", "prompt": "?"}
    out = werkzeuge.werkstatt_diagramm_setzen(TASK, "X", KNOTEN, KANTEN)
    assert out["ok"] is False and "offene" in out["fehler"] and not z.aufrufe


def test_setzen_meldet_wenn_graphviz_fehlt_oder_scheitert(welt: tuple[Zeichner, Learning, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _, lern, ordner = welt

    def kaputt(dot: str, art: str) -> bytes:
        raise zeichner.ZeichnerFehler("Graphviz ist auf diesem Rechner nicht eingerichtet")

    monkeypatch.setattr(werkzeuge, "_zeichnen", lambda: kaputt)
    out = werkzeuge.werkstatt_diagramm_setzen(TASK, "X", KNOTEN, KANTEN)
    assert out == {"ok": False, "fehler": "Graphviz ist auf diesem Rechner nicht eingerichtet"}
    assert not [r for r in lern.requests if r.method in ("PUT", "PATCH")] and not list(ordner.iterdir())


def test_setzen_lehnt_zu_grosses_oder_fremdes_ergebnis_ab(welt: tuple[Zeichner, Learning, Path]) -> None:
    z, lern, _ = welt
    for schlecht in (png(320, 480, fuell=zeichner.MAX_BYTES), b"GIF89a" + b"x" * 40, png(1200, 300)):
        z.png = schlecht
        out = werkzeuge.werkstatt_diagramm_setzen(TASK, "X", KNOTEN, KANTEN)
        assert out["ok"] is False, schlecht[:8]
    assert not [r for r in lern.requests if r.method in ("PUT", "PATCH")]


def test_aufgabe_aendern_setzt_kein_diagramm(welt: tuple[Zeichner, Learning, Path]) -> None:
    out = werkzeuge.werkstatt_aufgabe_aendern(TASK, 4, {"bild": {"herkunft": "diagramm"}}, "g")
    assert out["ok"] is False and "werkstatt_diagramm_setzen" in out["fehler"]


def test_werkzeug_ist_angemeldet() -> None:
    assert "werkstatt_diagramm_setzen" in {f.__name__ for f in server.WERKZEUGE}
    assert zeichner.ORDNER_ENV in server._ENV_KEYS
    hier = Path(werkzeuge.__file__).parent
    assert "mcp__learning__werkstatt_diagramm_setzen" in (hier / "learning-mcp.json").read_text(encoding="utf-8")
    assert "werkstatt_diagramm_setzen" in (hier / "gateway" / "anbinden.sh").read_text(encoding="utf-8")
    assert "werkstatt_diagramm_setzen" in (hier / "AGENTS.md").read_text(encoding="utf-8")


# ---- Bausteine ---------------------------------------------------------------------------------------------

def test_png_masse_aus_dem_kopf() -> None:
    assert zeichner.png_masse(png(640, 200)) == (640, 200)
    for fremd in (b"", b"GIF89a" + b"x" * 40, b"\x89PNG\r\n\x1a\n" + b"x" * 4):
        assert zeichner.png_masse(fremd) is None


def test_dateiname_aus_titel_und_inhalt() -> None:
    assert zeichner.dateiname("Ablauf der Mitose: Übersicht!", b"abc") == (
        f"ablauf-der-mitose-uebersicht-{hashlib.sha256(b'abc').hexdigest()[:8]}.html")
    assert zeichner.dateiname("../../etc/passwd", b"abc").startswith("etc-passwd-")
    assert zeichner.dateiname("???", b"abc") == f"diagramm-{hashlib.sha256(b'abc').hexdigest()[:8]}.html"
    assert len(zeichner.dateiname("x" * 300, b"abc")) <= 80


def test_ordner_ohne_angabe_liegt_neben_den_einstellungen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(zeichner.ORDNER_ENV, raising=False)
    monkeypatch.setenv("LEARNING_CLAW_ENV_FILE", str(tmp_path / "learning.env"))
    assert zeichner.ordner() == tmp_path / "diagramme"


def test_rendern_ruft_graphviz_mit_frist_und_ohne_dateizugriff(monkeypatch: pytest.MonkeyPatch) -> None:
    gesehen: dict[str, Any] = {}

    def run(befehl: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        gesehen.update(befehl=befehl, **kw)
        return subprocess.CompletedProcess(befehl, 0, stdout=b"bild", stderr=b"")

    monkeypatch.setattr(zeichner, "dot_pfad", lambda: "C:/x/dot.exe")
    monkeypatch.setattr(zeichner.subprocess, "run", run)
    assert zeichner.rendern("digraph G {}", "png") == b"bild"
    assert gesehen["befehl"] == ["C:/x/dot.exe", "-Tpng"] and gesehen["input"] == b"digraph G {}"
    assert gesehen["timeout"] == zeichner.FRIST_S == 10 and gesehen["capture_output"] is True
    # Der Text kommt über die Eingabe, nie als Datei; die Umgebung nennt Graphviz keinen Bildordner.
    assert "GV_FILE_PATH" not in gesehen["env"] and "GDFONTPATH" not in gesehen["env"]
    with pytest.raises(zeichner.ZeichnerFehler):
        zeichner.rendern("digraph G {}", "pdf")


def test_rendern_meldet_fehlendes_graphviz_abbruch_und_fehler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zeichner, "dot_pfad", lambda: None)
    with pytest.raises(zeichner.ZeichnerFehler, match="nicht eingerichtet"):
        zeichner.rendern("digraph G {}", "png")
    monkeypatch.setattr(zeichner, "dot_pfad", lambda: "dot")

    def frist(befehl: list[str], **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(befehl, 10)

    monkeypatch.setattr(zeichner.subprocess, "run", frist)
    with pytest.raises(zeichner.ZeichnerFehler, match="zu lange"):
        zeichner.rendern("digraph G {}", "png")
    monkeypatch.setattr(zeichner.subprocess, "run",
                        lambda befehl, **kw: subprocess.CompletedProcess(befehl, 1, stdout=b"", stderr=b"syntax error"))
    with pytest.raises(zeichner.ZeichnerFehler, match="nicht zeichnen"):
        zeichner.rendern("digraph G {}", "png")


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz ist auf diesem Rechner nicht installiert")
def test_echtes_graphviz_zeichnet_png_und_svg_in_den_grenzen() -> None:
    from spaces.learning.services.werkstatt.diagramm import Diagramm, dot_text

    viele = [{"id": f"k{i}", "text": f"Schritt {i} mit einem längeren Text"} for i in range(30)]
    dot = dot_text(Diagramm.model_validate({
        "richtung": "links-rechts", "knoten": viele,
        "kanten": [{"von": f"k{i}", "nach": f"k{i + 1}", "text": "weiter"} for i in range(29)],
        "gruppen": [{"titel": "Anfang", "knoten": ["k0", "k1"]}]}))
    bild = zeichner.rendern(dot, "png")
    masse = zeichner.png_masse(bild)
    # Auch die längste Kette bleibt in der Breite der Fläche.
    assert masse is not None and masse[0] <= zeichner.MAX_BREITE and len(bild) <= zeichner.MAX_BYTES
    svg = zeichner.rendern(dot, "svg").decode("utf-8")
    assert "<svg" in svg and "Schritt 29" in svg and "ä" in svg and "<script" not in svg
