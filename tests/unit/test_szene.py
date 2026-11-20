"""KI-erzeugte Szene an der Aufgabe: Inhalt, Quellenzeile, was Lernende sehen und der Hinweis in der Freigabe."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from spaces.learning.services.werkstatt.aufgabenbild import quellenzeile
from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
from spaces.learning.services.werkstatt.katalog import _item_felder
from spaces.learning.services.werkstatt.vorschau import vorschau
from spaces.learning.tests.unit.test_aufgabenbild import BILD, offen, skizze
from spaces.learning.tests.unit.test_diagramm import DIAGRAMM, EIGEN
from spaces.learning.tests.unit.test_werkstatt_freigabe import _aufgabe, _befund, _store, _vorbereiten

SZENE: dict[str, Any] = {
    "image": "/lernsequenz-medien/bild-0123456789abcdef.jpg", "width": 1024, "height": 768,
    "titel": "Besprechung im Projektteam", "herkunft": "ki",
    "beschreibung": "Five colleagues in a meeting room, two of them looking at their phones",
    "modell": "FLUX.2 klein",
}


def fall(**over: Any) -> dict[str, Any]:
    return {"kind": "case", "prompt": "Beschreiben Sie, was in dieser Besprechung schiefläuft.", "concept_ids": ["k1"],
            "expected_answer": "Zwei Personen sind abgelenkt, niemand führt Protokoll.",
            "rubric": [{"criterion_id": "r1", "description": "Ablenkung benannt", "points": 1}], **over}


def test_offene_aufgabe_und_fall_tragen_eine_ki_szene() -> None:
    bild = inhalt_lesen(offen(bild=SZENE)).bild
    assert bild.herkunft == "ki" and bild.beschreibung.startswith("Five colleagues") and bild.modell == "FLUX.2 klein"
    assert bild.lizenz is None and bild.urheber is None and bild.quelle_url is None and bild.diagramm is None
    assert inhalt_lesen(fall(bild=SZENE)).bild.herkunft == "ki"
    # Die anderen Herkünfte bleiben, wie sie waren.
    assert inhalt_lesen(offen(bild=BILD)).bild.beschreibung is None
    assert inhalt_lesen(offen(bild=EIGEN)).bild.modell is None


def test_skizze_traegt_keine_ki_szene() -> None:
    # An einer Skizze wird etwas Bestimmtes beschriftet; dafür zeichnet die Bild-KI zu ungenau.
    with pytest.raises(ValidationError):
        inhalt_lesen(skizze(bild=SZENE))
    assert inhalt_lesen(skizze(bild=BILD)).bild is not None and inhalt_lesen(skizze(bild=EIGEN)).bild is not None


@pytest.mark.parametrize("bild", [
    {k: v for k, v in SZENE.items() if k != "beschreibung"},
    {**SZENE, "beschreibung": ""},
    {**SZENE, "beschreibung": "x" * 601},
    {k: v for k, v in SZENE.items() if k != "modell"},
    {**SZENE, "lizenz": "CC0"},  # ein erzeugtes Bild behauptet keine fremde Lizenz
    {**SZENE, "urheber": "Jemand"},
    {**SZENE, "quelle_url": "https://commons.wikimedia.org/wiki/File:X.png"},
    {**SZENE, "diagramm": DIAGRAMM},
    {**BILD, "beschreibung": "eine Szene"},  # Commons-Bild mit Bildbeschreibung
    {**BILD, "modell": "FLUX.2 klein"},
    {**EIGEN, "beschreibung": "eine Szene"},
])
def test_herkunft_ki_und_angaben_muessen_zusammenpassen(bild: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        inhalt_lesen(offen(bild=bild))


def test_quellenzeile_sagt_dass_das_bild_ki_erzeugt_ist() -> None:
    assert quellenzeile(inhalt_lesen(offen(bild=SZENE)).bild) == "Bild: KI-erzeugt"


def test_katalog_traegt_die_szene_mit_dem_vermerk_ki() -> None:
    _typ, _erwartet, config, _rubrik = _item_felder("item-1", inhalt_lesen(fall(bild=SZENE)))
    assert config["presentation"] == {
        "image": SZENE["image"], "width": 1024, "height": 768, "bildquelle": {"ki": True},
    }


def test_vorschau_zeigt_die_szene_mit_quellenzeile() -> None:
    v = vorschau(offen(bild=SZENE))
    assert v["bild"] == SZENE["image"] and v["text"].endswith("Bild: KI-erzeugt")


def test_freigabe_weist_auf_ki_bilder_hin_ohne_zu_sperren(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _aufgabe(s, offen(bild=SZENE), kapitel="Kapitel 1")
    _aufgabe(s, offen(bild=BILD), kapitel="Kapitel 2")
    _aufgabe(s, offen(bild=EIGEN), kapitel="Kapitel 3")
    r = _vorbereiten(s)
    hinweise = _befund(r, "bild_ki")
    assert len(hinweise) == 1 and hinweise[0]["blockierend"] is False
    assert "Kapitel 1" in hinweise[0]["text"] and "KI-erzeugt" in hinweise[0]["text"]
    assert hinweise[0]["task_id"] is not None
