"""Diagramm an der Aufgabe: Kästen und Pfeile, der Graphviz-Text daraus und was Lernende davon sehen."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from spaces.learning.services.werkstatt.aufgabenbild import quellenzeile
from spaces.learning.services.werkstatt.diagramm import Diagramm, dot_text
from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
from spaces.learning.services.werkstatt.katalog import _item_felder
from spaces.learning.services.werkstatt.vorschau import vorschau
from spaces.learning.tests.unit.test_aufgabenbild import BILD, offen

DIAGRAMM: dict[str, Any] = {
    "richtung": "oben-unten",
    "knoten": [{"id": "pro", "text": "Prophase"}, {"id": "meta", "text": "Metaphase", "leer": True},
               {"id": "ana", "text": "Anaphase"}],
    "kanten": [{"von": "pro", "nach": "meta"}, {"von": "meta", "nach": "ana", "text": "Trennung"}],
    "gruppen": [],
}
EIGEN: dict[str, Any] = {
    "image": "/lernsequenz-medien/bild-0123456789abcdef.png", "width": 400, "height": 600,
    "titel": "Ablauf der Mitose", "herkunft": "diagramm", "diagramm": DIAGRAMM,
}


def diagramm(**over: Any) -> dict[str, Any]:
    return {**DIAGRAMM, **over}


# ---- Die Liste aus Kästen und Pfeilen ----------------------------------------------------------------------

def test_diagramm_aus_kaesten_und_pfeilen() -> None:
    d = Diagramm.model_validate(DIAGRAMM)
    assert [k.id for k in d.knoten] == ["pro", "meta", "ana"] and d.knoten[1].leer is True
    assert d.kanten[1].text == "Trennung" and d.kanten[0].text is None
    # Richtung und Gruppen haben Vorgaben.
    klein = Diagramm.model_validate({"knoten": [{"id": "a", "text": "A"}]})
    assert klein.richtung == "oben-unten" and klein.kanten == [] and klein.gruppen == []


@pytest.mark.parametrize("falsch", [
    diagramm(knoten=[]),
    diagramm(knoten=[{"id": "a", "text": "A"}, {"id": "a", "text": "B"}], kanten=[]),  # Kennung doppelt
    diagramm(knoten=[{"id": "A b", "text": "A"}], kanten=[]),  # Kennung nur klein, ohne Leerzeichen
    diagramm(knoten=[{"id": "a", "text": ""}], kanten=[]),
    diagramm(knoten=[{"id": "a", "text": "x" * 81}], kanten=[]),
    diagramm(kanten=[{"von": "pro", "nach": "gibt_es_nicht"}]),
    diagramm(kanten=[{"von": "gibt_es_nicht", "nach": "pro"}]),
    diagramm(richtung="diagonal"),
    diagramm(gruppen=[{"titel": "Kern", "knoten": ["fremd"]}]),
    diagramm(gruppen=[{"titel": "A", "knoten": ["pro"]}, {"titel": "B", "knoten": ["pro"]}]),  # in zwei Gruppen
    diagramm(gruppen=[{"titel": "A", "knoten": []}]),
    diagramm(knoten=[{"id": f"k{i}", "text": "x"} for i in range(31)], kanten=[]),
    diagramm(kanten=[{"von": "pro", "nach": "meta"}] * 61),
    diagramm(unbekannt=1),
])
def test_ungueltiges_diagramm_wird_abgelehnt(falsch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Diagramm.model_validate(falsch)


# ---- Der Graphviz-Text -------------------------------------------------------------------------------------

def test_dot_text_zeichnet_kaesten_pfeile_und_richtung() -> None:
    dot = dot_text(Diagramm.model_validate(DIAGRAMM))
    assert dot.startswith("digraph G {") and dot.rstrip().endswith("}")
    assert "rankdir=TB" in dot
    assert 'n0 [label="Prophase"]' in dot and 'n2 [label="Anaphase"]' in dot
    assert "n0 -> n1;" in dot and 'n1 -> n2 [label="Trennung"];' in dot
    assert "rankdir=LR" in dot_text(Diagramm.model_validate(diagramm(richtung="links-rechts")))


def test_leerer_kasten_traegt_eine_nummer_statt_der_loesung() -> None:
    d = Diagramm.model_validate(diagramm(knoten=[
        {"id": "pro", "text": "Prophase", "leer": True}, {"id": "meta", "text": "Metaphase"},
        {"id": "ana", "text": "Anaphase", "leer": True}]))
    dot = dot_text(d)
    # Die Lösung steht nirgends im Bild; nummeriert wird in der Reihenfolge der Liste.
    assert "Prophase" not in dot and "Anaphase" not in dot and "Metaphase" in dot
    assert 'n0 [label="1"' in dot and 'n2 [label="2"' in dot
    assert "dashed" in dot.split("n0 [", 1)[1].split("]", 1)[0]


def test_dot_text_nimmt_text_nur_als_text() -> None:
    # Weder Anführungszeichen noch Graphviz-Steuerfolgen noch Attribute kommen aus dem Text heraus.
    boese = 'A" image="C:/geheim.png" x="\\N \\G <b>fett</b>\nzweite'
    dot = dot_text(Diagramm.model_validate(diagramm(knoten=[{"id": "pro", "text": boese}, *DIAGRAMM["knoten"][1:]])))
    zeile = next(z for z in dot.splitlines() if z.strip().startswith("n0 ["))
    # Der Umbruch (\n klein) ist die einzige Steuerfolge, und sie kommt von uns.
    assert zeile.strip().replace("\\n", " ") == 'n0 [label="A\\" image=\\"C:/geheim.png\\" x=\\"\\\\N \\\\G <b>fett</b> zweite"];'
    assert dot.count("image") == 1  # nur im Text selbst, nie als Attribut


def test_dot_text_bricht_lange_texte_um() -> None:
    lang = "Die Schwesterchromatiden werden zu den Polen gezogen"
    dot = dot_text(Diagramm.model_validate(diagramm(knoten=[{"id": "pro", "text": lang}, *DIAGRAMM["knoten"][1:]])))
    zeile = next(z for z in dot.splitlines() if z.strip().startswith("n0 ["))
    teile = zeile.split('label="', 1)[1].rsplit('"', 1)[0].split("\\n")
    assert len(teile) >= 2 and all(len(t) <= 24 for t in teile) and " ".join(teile) == lang


def test_gruppen_fassen_kaesten_zusammen() -> None:
    dot = dot_text(Diagramm.model_validate(diagramm(gruppen=[{"titel": "Kernteilung", "knoten": ["pro", "meta"]}])))
    block = dot.split("subgraph cluster_0 {", 1)[1].split("}", 1)[0]
    assert 'label="Kernteilung"' in block and "n0;" in block and "n1;" in block and "n2;" not in block


def test_die_kennungen_des_modells_stehen_nicht_im_graphviz_text() -> None:
    # Eigene Kennungen n0, n1, …: was das Modell als Kennung schreibt, erreicht Graphviz nie.
    dot = dot_text(Diagramm.model_validate(diagramm(
        knoten=[{"id": "node", "text": "A"}, {"id": "edge", "text": "B"}], kanten=[{"von": "node", "nach": "edge"}])))
    assert "node ->" not in dot and "n0 -> n1;" in dot


# ---- Das Diagramm als Bild der Aufgabe ---------------------------------------------------------------------

def test_aufgabe_traegt_ein_diagramm_ohne_lizenz_und_urheber() -> None:
    bild = inhalt_lesen(offen(bild=EIGEN)).bild
    assert bild.herkunft == "diagramm" and bild.diagramm.knoten[0].text == "Prophase"
    assert bild.lizenz is None and bild.urheber is None and bild.quelle_url is None
    # Ein Bild von Commons bleibt, wie es war; die Herkunft ist dann die Vorgabe.
    assert inhalt_lesen(offen(bild=BILD)).bild.herkunft == "commons"


@pytest.mark.parametrize("bild", [
    {**EIGEN, "diagramm": None},  # Diagramm ohne Liste
    {k: v for k, v in EIGEN.items() if k != "diagramm"},
    {**EIGEN, "lizenz": "CC0"},  # eine eigene Darstellung behauptet keine fremde Lizenz
    {**EIGEN, "urheber": "Jemand"},
    {**EIGEN, "quelle_url": "https://commons.wikimedia.org/wiki/File:X.png"},
    {**BILD, "diagramm": DIAGRAMM},  # Commons-Bild mit Liste
    {**BILD, "herkunft": "commons", "lizenz": None},
    {**BILD, "herkunft": "commons", "urheber": None},
    {**BILD, "herkunft": "commons", "quelle_url": None},
    {**BILD, "herkunft": "ki"},
])
def test_herkunft_und_angaben_muessen_zusammenpassen(bild: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        inhalt_lesen(offen(bild=bild))


def test_quellenzeile_nennt_die_eigene_darstellung() -> None:
    assert quellenzeile(inhalt_lesen(offen(bild=EIGEN)).bild) == "Diagramm: Ablauf der Mitose, eigene Darstellung"


def test_katalog_traegt_das_diagramm_ohne_die_liste() -> None:
    _typ, _erwartet, config, _rubrik = _item_felder("item-1", inhalt_lesen(offen(bild=EIGEN)))
    # Die Liste enthält die Texte der leeren Kästen, also die Lösung: sie geht nie zu den Lernenden.
    assert config["presentation"] == {
        "image": EIGEN["image"], "width": 400, "height": 600,
        "bildquelle": {"titel": "Ablauf der Mitose", "eigen": True},
    }


def test_vorschau_zeigt_das_diagramm_mit_quellenzeile() -> None:
    v = vorschau(offen(bild=EIGEN))
    assert v["bild"] == EIGEN["image"] and v["text"].endswith("Diagramm: Ablauf der Mitose, eigene Darstellung")
