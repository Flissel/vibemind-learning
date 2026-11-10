"""Anordnung der Aufgabenbereiche und Markenzeile."""

from __future__ import annotations

import pytest

from spaces.learning.huelle.anordnung import ZuVieleAufgaben, anordnung, markenzeile, zeile


def test_anordnung_eine_spalte() -> None:
    bereiche = anordnung([False] * 3)
    assert [b["y"] for b in bereiche] == [100, 1050, 2000]
    assert all(b["x"] == 100 and b["w"] == 1300 and b["h"] == 800 for b in bereiche)


def test_grosse_aufgabe_bekommt_mehr_platz() -> None:
    bereiche = anordnung([False, True, False])
    assert [b["h"] for b in bereiche] == [800, 1100, 800]
    assert [b["y"] for b in bereiche] == [100, 1050, 2300]


def test_zweite_spalte() -> None:
    bereiche = anordnung([False] * 22)
    assert bereiche[20]["y"] == 19_100
    assert bereiche[20]["y"] + bereiche[20]["h"] == 19_900
    assert bereiche[21]["x"] == 3000 and bereiche[21]["y"] == 100


def test_zu_viele_aufgaben() -> None:
    assert len(anordnung([False] * 42)) == 42
    with pytest.raises(ZuVieleAufgaben):
        anordnung([False] * 43)


def test_leere_liste() -> None:
    assert anordnung([]) == []


def test_markenzeile() -> None:
    bereiche = anordnung([False, False])
    assert markenzeile(bereiche) == "<!-- lernflaeche-bereiche: [[100,100,2100,800],[100,1050,2100,800]] -->"
    assert zeile(bereiche[0]) == {"x": 100, "y": 100, "w": 2100, "h": 800}


def test_zeile_endet_mit_der_karte_und_beruehrt_die_zweite_spalte_nicht() -> None:
    # A3: die Karte rechts neben dem Bereich ist breiter (mehr Text). Die Zeile (Rahmen der Aufgabenwahl)
    # reicht genau bis zu ihrem Ende; Karte und Berichtsbild bleiben links der zweiten Spalte.
    from spaces.learning.huelle.anordnung import SPALTEN_X
    from spaces.learning.huelle.penecho_mcp import felder

    for b in anordnung([False, True] * 12):
        f = felder(b)
        z = zeile(b)
        assert z["x"] + z["w"] == f.rueckmeldungsfeld["x"] + f.rueckmeldungsfeld["w"]
        if b["x"] == SPALTEN_X[0]:
            assert f.berichtsplatz["x"] + f.berichtsplatz["w"] < SPALTEN_X[1]
