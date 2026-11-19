"""Bild an der Aufgabe: Lizenzregel, Inhalt, Katalog, Vorschau und was Lernende davon sehen."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.werkstatt.aufgabenbild import lizenz_erlaubt, quellenzeile
from spaces.learning.services.werkstatt.freigabe import inhalt_hash, loesung_hash
from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
from spaces.learning.services.werkstatt.katalog import _item_felder
from spaces.learning.services.werkstatt.vorschau import vorschau
from spaces.learning.tests.unit.test_vorbewertung import NOW, _Rubrik, _rubric_item, _start, _store

BILD: dict[str, Any] = {
    "image": "/lernsequenz-medien/bild-0123456789abcdef.png", "width": 1000, "height": 750,
    "titel": "Chromosome.svg", "urheber": "Magnus Manske", "lizenz": "CC BY-SA 3.0",
    "lizenz_url": "https://creativecommons.org/licenses/by-sa/3.0", "quelle_url": "https://commons.wikimedia.org/wiki/File:Chromosome.svg",
}
RUBRIK = [{"criterion_id": f"r{i}", "description": f"Teil {i} richtig benannt", "points": 1} for i in (1, 2, 3)]


def offen(**over: Any) -> dict[str, Any]:
    return {"kind": "open", "prompt": "Benennen Sie die Teile des Chromosoms.", "concept_ids": ["k1"],
            "expected_answer": "Zentromer, Chromatid, Telomer", "rubric": RUBRIK, **over}


def skizze(**over: Any) -> dict[str, Any]:
    return {"kind": "sketch", "prompt": "Beschriften Sie das Chromosom.", "concept_ids": ["k1"],
            "expected_answer": "Zentromer in der Mitte, Telomere an den Enden", "rubric": RUBRIK, **over}


@pytest.mark.parametrize("kurz", [
    "CC0", "CC0 1.0", "Public domain", "CC BY 4.0", "CC BY-SA 4.0", "CC BY-SA 3.0", "CC BY 2.5", "CC BY-SA 3.0 de",
    "cc by-sa 4.0",
])
def test_freie_lizenzen_sind_erlaubt(kurz: str) -> None:
    assert lizenz_erlaubt(kurz) is True


@pytest.mark.parametrize("kurz", [
    "CC BY-NC 4.0", "CC BY-NC-SA 3.0", "CC BY-ND 4.0", "CC BY-NC-ND 2.0", "GFDL", "Copyrighted free use",
    "Fair use", "", "CC BY", "All rights reserved", "CC BY-SA 4.0 or later, non-commercial", "CC BYX 4.0",
])
def test_alles_andere_ist_nicht_erlaubt(kurz: str) -> None:
    # Keine Nutzung nur fuer Nichtgewerbliches, kein Bearbeitungsverbot, nichts Unbekanntes: im Zweifel nein.
    assert lizenz_erlaubt(kurz) is False


def test_offene_aufgabe_und_skizze_tragen_ein_bild() -> None:
    assert inhalt_lesen(offen(bild=BILD)).bild.lizenz == "CC BY-SA 3.0"
    assert inhalt_lesen(skizze(bild=BILD)).bild.titel == "Chromosome.svg"
    assert inhalt_lesen(offen()).bild is None
    # Gemeinfrei ohne Lizenzadresse geht; ein Urheber steht trotzdem da (notfalls "unbekannt").
    frei = {**BILD, "lizenz": "Public domain", "lizenz_url": None, "urheber": "unbekannt"}
    assert inhalt_lesen(offen(bild=frei)).bild.lizenz_url is None


@pytest.mark.parametrize("feld,wert", [
    ("lizenz", "CC BY-NC 4.0"),
    ("lizenz", ""),
    ("urheber", ""),
    ("image", "/lernsequenz-medien/term-0123456789abcdef.png"),  # nur eigens abgelegte Bilder
    ("image", "https://example.org/bild.png"),
    ("image", "/lernsequenz-medien/bild-0123456789abcdef.svg"),
    ("quelle_url", "https://example.org/irgendwo"),  # nur Wikimedia Commons
    ("lizenz_url", "javascript:alert(1)"),
    ("width", 0),
])
def test_ungueltiges_bild_wird_nicht_gespeichert(feld: str, wert: Any) -> None:
    with pytest.raises(ValidationError):
        inhalt_lesen(offen(bild={**BILD, feld: wert}))


def test_andere_aufgabentypen_tragen_kein_bild() -> None:
    quiz = {"kind": "quiz", "prompt": "?", "concept_ids": ["k1"], "correct_option_ids": ["a"],
            "options": [{"option_id": "a", "text": "ja"}, {"option_id": "b", "text": "nein"}], "bild": BILD}
    with pytest.raises(ValidationError):
        inhalt_lesen(quiz)


def test_ein_bild_aendert_den_inhalt_aber_nicht_die_loesung() -> None:
    # Fuer die Freigabe ist ein neues Bild eine Textkorrektur: der Lernstand bleibt.
    assert loesung_hash(offen(bild=BILD)) == loesung_hash(offen())
    assert inhalt_hash(offen(bild=BILD)) != inhalt_hash(offen())


def test_quellenzeile_nennt_titel_urheber_lizenz_und_herkunft() -> None:
    assert quellenzeile(inhalt_lesen(offen(bild=BILD)).bild) == (
        "Bild: Chromosome.svg, Magnus Manske, CC BY-SA 3.0, Wikimedia Commons")


@pytest.mark.parametrize("bauen,typ", [(offen, "open"), (skizze, "penecho_canvas")])
def test_katalog_traegt_bild_und_quellenangabe(bauen: Any, typ: str) -> None:
    item_type, _erwartet, config, _rubrik = _item_felder("item-1", inhalt_lesen(bauen(bild=BILD)))
    assert item_type == typ
    assert config["presentation"] == {
        "image": BILD["image"], "width": 1000, "height": 750,
        "bildquelle": {"titel": "Chromosome.svg", "urheber": "Magnus Manske", "lizenz": "CC BY-SA 3.0",
                       "lizenz_url": BILD["lizenz_url"], "quelle_url": BILD["quelle_url"]},
    }
    assert "presentation" not in _item_felder("item-1", inhalt_lesen(bauen()))[2]


def test_vorschau_zeigt_bild_und_quellenangabe() -> None:
    v = vorschau(offen(bild=BILD))
    assert v["bild"] == BILD["image"]
    assert v["text"].endswith("Bild: Chromosome.svg, Magnus Manske, CC BY-SA 3.0, Wikimedia Commons")
    assert vorschau(skizze(bild=BILD))["bild"] == BILD["image"]
    assert vorschau(offen())["bild"] is None and "Bild:" not in vorschau(offen())["text"]


@pytest.mark.parametrize("item_type", ["open", "penecho_canvas"])
def test_lernende_bekommen_bild_und_quellenangabe(tmp_path: Path, item_type: str) -> None:
    spec = _rubric_item(item_type)
    config = dict(spec["scoring_config"])  # type: ignore[arg-type]
    quelle = {"titel": "Chromosome.svg", "urheber": "Magnus Manske", "lizenz": "CC BY-SA 3.0",
              "lizenz_url": BILD["lizenz_url"], "quelle_url": BILD["quelle_url"]}
    config["presentation"] = {"image": BILD["image"], "width": 1000, "height": 750, "bildquelle": quelle,
                              "geheim": "darf nie durch"}
    engine, factory, course_id, _ = _store(tmp_path, [{**spec, "scoring_config": config}] * 2)
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=_Rubrik())
    darstellung = _start(service, course_id).task.presentation
    assert darstellung["image"] == BILD["image"] and (darstellung["width"], darstellung["height"]) == (1000, 750)
    assert darstellung["bildquelle"] == quelle and "geheim" not in darstellung
    engine.dispose()
