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
    assert markenzeile(bereiche) == "<!-- lernflaeche-bereiche: [[100,100,1760,800],[100,1050,1760,800]] -->"
    assert zeile(bereiche[0]) == {"x": 100, "y": 100, "w": 1760, "h": 800}
