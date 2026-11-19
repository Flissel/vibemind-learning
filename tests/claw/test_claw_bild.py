"""Bild an der Aufgabe: Suche bei Wikimedia Commons und Ablegen mit Lizenz (nachgestelltes Commons und Learning)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from spaces.learning.claw import commons, server, werkzeuge
from spaces.learning.claw.learning_client import LearningClient

KEY = "k" * 40
API = "http://127.0.0.1:8090/api/v1"
MCP = "http://127.0.0.1:8091/mcp"
TASK = "task-1"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 200
JPG = b"\xff\xd8\xff\xe0" + b"y" * 200
KONTAKT = "https://example.org/kurse"


def seite(titel: str, lizenz: str, *, artist: str | None = "<a href=\"//x\">Magnus  Manske</a>",
          mime: str = "image/svg+xml", thumb: str = "https://upload.wikimedia.org/thumb/a.png",
          lizenz_url: str | None = "https://creativecommons.org/licenses/by-sa/3.0", index: int = 1) -> dict[str, Any]:
    em: dict[str, Any] = {"LicenseShortName": {"value": lizenz}}
    if artist is not None:
        em["Artist"] = {"value": artist}
    if lizenz_url is not None:
        em["LicenseUrl"] = {"value": lizenz_url}
    return {"title": f"File:{titel}", "index": index, "imageinfo": [{
        "mime": mime, "width": 2000, "height": 1500, "thumburl": thumb, "thumbwidth": 1000, "thumbheight": 750,
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{titel}", "extmetadata": em,
    }]}


class Commons:
    """Nachgestelltes Commons: `seiten` fuer die API, `bilder` je Adresse fuer upload.wikimedia.org."""

    def __init__(self) -> None:
        self.seiten: list[dict[str, Any]] = []
        self.bilder: dict[str, bytes] = {"https://upload.wikimedia.org/thumb/a.png": PNG}
        self.anfragen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json={"query": {"pages": self.seiten}} if self.seiten else {"batchcomplete": True})
        daten = self.bilder.get(str(request.url))
        return httpx.Response(200, content=daten) if daten is not None else httpx.Response(404)


class Learning:
    def __init__(self, inhalt: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.inhalt = inhalt or {"kind": "open", "prompt": "Benennen Sie.", "concept_ids": ["k1"],
                                 "expected_answer": "x", "rubric": []}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"fassungen": [{"number": 4, "content": self.inhalt}]})
        if request.method == "PUT":
            return httpx.Response(200, json={"name": request.url.path.rsplit("/", 1)[-1], "bytes": len(request.content)})
        return httpx.Response(200, json={"number": 5, "art": "textkorrektur", "findings": []})


@pytest.fixture
def netz(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Commons, Learning]]:
    c, lern = Commons(), Learning()
    monkeypatch.setenv(commons.KONTAKT_ENV, KONTAKT)
    monkeypatch.setattr(werkzeuge, "_commons_transport", lambda: httpx.MockTransport(c.handle))
    monkeypatch.setattr(werkzeuge, "_client", lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(lern.handle)))
    yield c, lern


# ---- Suche -------------------------------------------------------------------------------------------------

def test_suche_nennt_nur_bilder_mit_freier_lizenz(netz: tuple[Commons, Learning]) -> None:
    c, _ = netz
    c.seiten = [
        seite("Chromosome.svg", "CC BY-SA 3.0", index=2),
        seite("Karyotyp.jpg", "CC BY-NC 4.0", index=1),
        seite("Zelle.png", "Public domain", artist=None, lizenz_url=None, mime="image/png", index=3),
        seite("Film.webm", "CC0", mime="video/webm", index=4),
        seite("Ohne_Urheber.png", "CC BY 4.0", artist=None, index=5),
    ]
    out = werkzeuge.werkstatt_bild_suchen("chromosom")
    assert out["ok"] is True
    treffer = out["daten"]["treffer"]
    # Reihenfolge der Suche; nicht frei, kein Bild oder ohne Urheber bei Namensnennung: nicht dabei.
    assert [t["datei"] for t in treffer] == ["File:Chromosome.svg", "File:Zelle.png"]
    assert treffer[0] == {
        "datei": "File:Chromosome.svg", "titel": "Chromosome.svg", "urheber": "Magnus Manske", "lizenz": "CC BY-SA 3.0",
        "seite": "https://commons.wikimedia.org/wiki/File:Chromosome.svg", "breite": 2000, "hoehe": 1500,
    }
    assert treffer[1]["urheber"] == "unbekannt"
    assert out["daten"]["aussortiert"] == 3
    anfrage = c.anfragen[0]
    assert anfrage.url.params["gsrsearch"] == "chromosom" and anfrage.url.params["gsrnamespace"] == "6"
    # Wikimedia verlangt Namen und Kontakt des Abrufers.
    assert KONTAKT in anfrage.headers["User-Agent"]


def test_suche_ohne_kontaktangabe_fragt_commons_nicht(netz: tuple[Commons, Learning], monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = netz
    monkeypatch.delenv(commons.KONTAKT_ENV)
    out = werkzeuge.werkstatt_bild_suchen("chromosom")
    assert out["ok"] is False and commons.KONTAKT_ENV in out["fehler"] and not c.anfragen


def test_suche_meldet_wenn_commons_ablehnt(netz: tuple[Commons, Learning], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(werkzeuge, "_commons_transport", lambda: httpx.MockTransport(lambda r: httpx.Response(403, text="robot policy")))
    out = werkzeuge.werkstatt_bild_suchen("chromosom")
    assert out == {"ok": False, "fehler": "Wikimedia Commons ist nicht erreichbar oder lehnt die Anfrage ab (403)"}


# ---- Setzen ------------------------------------------------------------------------------------------------

def test_setzen_legt_die_kopie_ab_und_traegt_bild_mit_lizenz_ein(netz: tuple[Commons, Learning]) -> None:
    c, lern = netz
    c.seiten = [seite("Chromosome.svg", "CC BY-SA 3.0")]
    out = werkzeuge.werkstatt_bild_setzen(TASK, "File:Chromosome.svg")
    assert out["ok"] is True, out
    name = f"bild-{hashlib.sha256(PNG).hexdigest()[:16]}.png"
    put = next(r for r in lern.requests if r.method == "PUT")
    assert put.url.path == f"/api/v1/lernsequenz-medien/{name}" and put.content == PNG
    assert put.headers["X-VibeMind-Roles"] == "learning.author"
    patch = next(r for r in lern.requests if r.method == "PATCH")
    body = json.loads(patch.content)
    assert body["based_on"] == 4 and "Chromosome.svg" in body["reason"] and "CC BY-SA 3.0" in body["reason"]
    assert body["felder"] == {"bild": {
        "image": f"/lernsequenz-medien/{name}", "width": 1000, "height": 750,
        "titel": "Chromosome.svg", "urheber": "Magnus Manske", "lizenz": "CC BY-SA 3.0",
        "lizenz_url": "https://creativecommons.org/licenses/by-sa/3.0",
        "quelle_url": "https://commons.wikimedia.org/wiki/File:Chromosome.svg",
    }}
    assert out["daten"] == {"fassung": 5, "bild": name, "quelle": "Bild: Chromosome.svg, Magnus Manske, CC BY-SA 3.0, Wikimedia Commons"}
    # Die Datei wird beim Namen gefragt, nicht gesucht: Lizenz und Urheber kommen von Commons.
    assert c.anfragen[0].url.params["titles"] == "File:Chromosome.svg"


@pytest.mark.parametrize("lizenz", ["CC BY-NC 4.0", "GFDL", ""])
def test_setzen_lehnt_unfreie_lizenz_ab_und_schreibt_nichts(netz: tuple[Commons, Learning], lizenz: str) -> None:
    c, lern = netz
    c.seiten = [seite("Karyotyp.jpg", lizenz)]
    out = werkzeuge.werkstatt_bild_setzen(TASK, "File:Karyotyp.jpg")
    assert out["ok"] is False and "Lizenz" in out["fehler"]
    assert not [r for r in lern.requests if r.method in ("PUT", "PATCH")]


def test_setzen_nur_an_offener_aufgabe_fall_oder_skizze(netz: tuple[Commons, Learning]) -> None:
    c, lern = netz
    c.seiten = [seite("Chromosome.svg", "CC BY-SA 3.0")]
    lern.inhalt = {"kind": "quiz", "prompt": "?"}
    out = werkzeuge.werkstatt_bild_setzen(TASK, "File:Chromosome.svg")
    assert out["ok"] is False and "offene" in out["fehler"]
    assert not c.anfragen and not [r for r in lern.requests if r.method in ("PUT", "PATCH")]


def test_setzen_meldet_unbekannte_datei_und_fremde_bildadresse(netz: tuple[Commons, Learning]) -> None:
    c, lern = netz
    assert werkzeuge.werkstatt_bild_setzen(TASK, "File:Gibt_es_nicht.png")["ok"] is False
    c.seiten = [seite("Chromosome.svg", "CC BY-SA 3.0", thumb="https://example.org/boese.png")]
    out = werkzeuge.werkstatt_bild_setzen(TASK, "File:Chromosome.svg")
    assert out["ok"] is False and not [r for r in lern.requests if r.method in ("PUT", "PATCH")]
    # Nur upload.wikimedia.org wird abgerufen.
    assert not [r for r in c.anfragen if r.url.host == "example.org"]


def test_setzen_nimmt_bei_zu_grossem_bild_eine_kleinere_fassung(
    netz: tuple[Commons, Learning], monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, lern = netz
    gross = "https://upload.wikimedia.org/thumb/gross.jpg"
    c.bilder[gross] = b"\xff\xd8\xff\xe0" + b"z" * (commons.MAX_BYTES + 1)
    c.seiten = [seite("Foto.jpg", "CC0", mime="image/jpeg", thumb=gross)]
    breiten: list[str] = []
    echt = c.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "commons.wikimedia.org":
            breiten.append(request.url.params["iiurlwidth"])
            if request.url.params["iiurlwidth"] != "1000":
                klein = seite("Foto.jpg", "CC0", mime="image/jpeg", thumb="https://upload.wikimedia.org/thumb/klein.jpg")
                klein["imageinfo"][0].update(thumbwidth=700, thumbheight=525)
                c.anfragen.append(request)
                return httpx.Response(200, json={"query": {"pages": [klein]}})
        return echt(request)

    c.bilder["https://upload.wikimedia.org/thumb/klein.jpg"] = JPG
    monkeypatch.setattr(werkzeuge, "_commons_transport", lambda: httpx.MockTransport(handle))
    out = werkzeuge.werkstatt_bild_setzen(TASK, "File:Foto.jpg")
    assert out["ok"] is True, out
    assert breiten == ["1000", "700"]
    bild = json.loads(next(r for r in lern.requests if r.method == "PATCH").content)["felder"]["bild"]
    assert bild["image"].endswith(".jpg") and (bild["width"], bild["height"]) == (700, 525)


def test_setzen_ohne_datei_nimmt_das_bild_weg(netz: tuple[Commons, Learning]) -> None:
    c, lern = netz
    out = werkzeuge.werkstatt_bild_setzen(TASK, None)
    assert out["ok"] is True and not c.anfragen
    assert json.loads(next(r for r in lern.requests if r.method == "PATCH").content)["felder"] == {"bild": None}


def test_aufgabe_aendern_setzt_kein_bild(netz: tuple[Commons, Learning]) -> None:
    # Sonst koennte ein Modell Lizenz und Urheber selbst hinschreiben.
    _, lern = netz
    out = werkzeuge.werkstatt_aufgabe_aendern(TASK, 4, {"prompt": "neu", "bild": {"lizenz": "CC0"}}, "g")
    assert out["ok"] is False and "werkstatt_bild_setzen" in out["fehler"] and not lern.requests


def test_werkzeuge_sind_angemeldet() -> None:
    namen = {f.__name__ for f in server.WERKZEUGE}
    assert {"werkstatt_bild_suchen", "werkstatt_bild_setzen"} <= namen
    # Der Dienst uebernimmt aus seiner Einstellungsdatei nur genannte Schluessel.
    assert commons.KONTAKT_ENV in server._ENV_KEYS


# ---- Bausteine ---------------------------------------------------------------------------------------------

def test_urheber_wird_von_html_befreit() -> None:
    assert commons.klartext('<a href="//x" title="User:M">Magnus  <b>Manske</b></a> &amp; Team\n') == "Magnus Manske & Team"
    assert commons.klartext(None) == ""


def test_bildart_nur_png_und_jpeg() -> None:
    assert commons.bildart(PNG) == "png" and commons.bildart(JPG) == "jpg"
    for fremd in (b"<svg xmlns=", b"GIF89a....", b"RIFF....WEBP", b""):
        assert commons.bildart(fremd) is None
