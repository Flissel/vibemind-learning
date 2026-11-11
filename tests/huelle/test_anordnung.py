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


# ---- Teil C: Mappen auf der Gruppenflaeche ------------------------------------------------------------

def test_gruppen_raster_eine_spalte() -> None:
    from spaces.learning.huelle.anordnung import gruppen_raster

    # Kopf 150 + 5 Aufgaben je 800 mit Abstand 150 = 4750 hoch, eine Spalte des Solo-Rasters breit (2100).
    r = gruppen_raster([800] * 5)
    assert (r.breite, r.hoehe) == (2100, 4750)
    # Platzraster: Mappe + 200 je Richtung, auf 19 800 verteilt.
    assert (r.spalten, r.zeilen) == (19_800 // 2300, 19_800 // 4950) == (8, 4)


def test_gruppen_raster_zwei_spalten_ab_19600() -> None:
    from spaces.learning.huelle.anordnung import gruppen_raster, mappen_bereiche

    # Genau 19 600 (Kopf + 18 x 800 + 2 x 1100 + 19 Abstaende): noch eine Spalte.
    knapp = [800] * 18 + [1100] * 2
    r = gruppen_raster(knapp)
    assert (r.breite, r.hoehe, r.spalten, r.zeilen) == (2100, 19_600, 8, 1)
    # Eine Aufgabe mehr waere hoeher als 19 600: zwei Spalten im Abstand 2900, Breite 5000.
    r = gruppen_raster(knapp + [800])
    assert r.breite == 5000 and r.spalten == 19_800 // 5200 == 3 and r.zeilen == 1
    bereiche = mappen_bereiche(knapp + [800], (100, 100))
    assert {b["x"] for b in bereiche} == {100, 3000}
    assert max(b["y"] + b["h"] for b in bereiche) <= 100 + r.hoehe  # die Mappe umfasst alle Bereiche
    assert r.hoehe <= 19_600


def test_mappen_ursprung_zeilenweise() -> None:
    from spaces.learning.huelle.anordnung import gruppen_raster, mappen_bereiche, mappen_ursprung

    r = gruppen_raster([800] * 5)  # 8 Spalten, 4 Zeilen, Platz 2300 x 4950
    assert mappen_ursprung(0, r) == (100, 100)
    assert mappen_ursprung(1, r) == (2400, 100)
    assert mappen_ursprung(7, r) == (100 + 7 * 2300, 100)
    assert mappen_ursprung(8, r) == (100, 100 + 4950)
    assert mappen_ursprung(31, r) == (100 + 7 * 2300, 100 + 3 * 4950)
    # Die Bereiche sind die Solo-Bereiche, verschoben um den Ursprung plus den Kopf (0, 150).
    solo = anordnung([False] * 5)
    assert mappen_bereiche([800] * 5, (2400, 100)) == [
        {**b, "x": 2400 + b["x"] - 100, "y": 100 + 150 + b["y"] - 100} for b in solo]
    # Die letzte Mappe bleibt auf der 20 000er-Flaeche.
    x, y = mappen_ursprung(31, r)
    assert x + r.breite <= 20_000 and y + r.hoehe <= 20_000


def test_mappe_voll() -> None:
    from spaces.learning.huelle.anordnung import MappeVoll, gruppen_raster, mappen_ursprung

    r = gruppen_raster([800] * 5)
    mappen_ursprung(r.spalten * r.zeilen - 1, r)
    with pytest.raises(MappeVoll):
        mappen_ursprung(r.spalten * r.zeilen, r)
    with pytest.raises(MappeVoll):
        mappen_ursprung(-1, r)
