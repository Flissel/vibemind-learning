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

import itertools  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from spaces.learning.huelle.anordnung import (  # noqa: E402
    KOPF, GruppenRaster, MappeVoll, _altes_raster, gruppen_kapazitaet, gruppen_raster, mappen_abschluss,
    mappen_bereiche, mappen_kopf, mappen_ursprung, passt_in,
)

_VEKTOR = Path(__file__).with_name("raster_alt_vektor.json")


def _r(b: dict[str, int]) -> list[int]:
    return [b["x"], b["y"], b["w"], b["h"]]


def _getrennt(a: dict[str, int], b: dict[str, int]) -> bool:
    return (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
            or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


# -- altes Raster (feste Raster laufender Gruppen) --

def test_altes_raster_eine_spalte() -> None:
    # Kopf 150 + 5 Aufgaben je 800 mit Abstand 150 = 4750 hoch, eine Spalte des Solo-Rasters breit (2100).
    r = _altes_raster([800] * 5)
    assert (r.breite, r.hoehe) == (2100, 4750)
    # Platzraster: Mappe + 200 je Richtung, auf 19 800 verteilt.
    assert (r.spalten, r.zeilen) == (19_800 // 2300, 19_800 // 4950) == (8, 4)
    assert (r.teilspalten, r.zugabe) == (None, 200)
    assert r == GruppenRaster(2100, 4750, 8, 4)  # die Vorgaben sind das alte Raster


def test_altes_raster_zwei_spalten_ab_19600() -> None:
    # Genau 19 600 (Kopf + 18 x 800 + 2 x 1100 + 19 Abstaende): noch eine Spalte.
    knapp = [800] * 18 + [1100] * 2
    r = _altes_raster(knapp)
    assert (r.breite, r.hoehe, r.spalten, r.zeilen) == (2100, 19_600, 8, 1)
    # Eine Aufgabe mehr waere hoeher als 19 600: zwei Spalten im Abstand 2900, Breite 5000.
    r = _altes_raster(knapp + [800])
    assert r.breite == 5000 and r.spalten == 19_800 // 5200 == 3 and r.zeilen == 1
    bereiche = mappen_bereiche(knapp + [800], (100, 100), r)
    assert {b["x"] for b in bereiche} == {100, 3000}
    assert max(b["y"] + b["h"] for b in bereiche) <= 100 + r.hoehe  # die Mappe umfasst alle Bereiche
    assert r.hoehe <= 19_600


def test_altes_raster_mappen_ursprung_zeilenweise() -> None:
    r = _altes_raster([800] * 5)  # 8 Spalten, 4 Zeilen, Platz 2300 x 4950
    assert mappen_ursprung(0, r) == (100, 100)
    assert mappen_ursprung(1, r) == (2400, 100)
    assert mappen_ursprung(7, r) == (100 + 7 * 2300, 100)
    assert mappen_ursprung(8, r) == (100, 100 + 4950)
    assert mappen_ursprung(31, r) == (100 + 7 * 2300, 100 + 3 * 4950)
    # Die Bereiche sind die Solo-Bereiche, verschoben um den Ursprung plus den Kopf (0, 150).
    solo = anordnung([False] * 5)
    erwartet = [{**b, "x": 2400 + b["x"] - 100, "y": 100 + 150 + b["y"] - 100} for b in solo]
    assert mappen_bereiche([800] * 5, (2400, 100), r) == erwartet
    assert mappen_bereiche([800] * 5, (2400, 100)) == erwartet  # ohne Raster wie bisher
    x, y = mappen_ursprung(31, r)
    assert x + r.breite <= 20_000 and y + r.hoehe <= 20_000


@pytest.mark.parametrize("fall", sorted(json.loads(_VEKTOR.read_text(encoding="utf-8"))))
def test_altes_festes_raster_rechnet_exakt_wie_vorher(fall: str) -> None:
    """PFLICHT: eine Gruppe mit altem festen Raster (vier Zahlen in gruppen-gesetzt.json) bekommt exakt dieselben
    Ursprünge, Bereiche, Köpfe und Abschlussplätze wie vor der Kapazitätsänderung (Prüfvektor aus dem alten Stand)."""
    daten = json.loads(_VEKTOR.read_text(encoding="utf-8"))[fall]
    h = daten["hoehen"]
    fest = GruppenRaster(**daten["raster"])  # so liest die Bruecke einen alten Eintrag
    assert fest == _altes_raster(h)
    assert passt_in(h, fest)
    assert fest.plaetze == len(daten["plaetze"])
    for platz, erwartet in enumerate(daten["plaetze"]):
        u = mappen_ursprung(platz, fest)
        assert list(u) == erwartet["ursprung"]
        assert [_r(b) for b in mappen_bereiche(h, u, fest)] == erwartet["bereiche"]
        assert _r(mappen_kopf(u)) == erwartet["kopf"]
        assert _r(mappen_abschluss(u)) == erwartet["abschluss"]
    with pytest.raises(MappeVoll):
        mappen_ursprung(fest.plaetze, fest)


def test_altes_festes_raster_passt_wie_bisher() -> None:
    fest = _altes_raster([800] * 3)
    assert passt_in([800] * 3, fest) and passt_in([800] * 2, fest)
    assert not passt_in([1100] * 3, fest)  # hoeher: voll
    assert not passt_in([800] * 4, fest)
    with pytest.raises(ZuVieleAufgaben):  # drei Spalten: wie bisher
        passt_in([800] * 43, fest)


def test_mappe_voll() -> None:
    r = gruppen_raster([800] * 5)
    mappen_ursprung(r.spalten * r.zeilen - 1, r)
    with pytest.raises(MappeVoll):
        mappen_ursprung(r.spalten * r.zeilen, r)
    with pytest.raises(MappeVoll):
        mappen_ursprung(-1, r)


# -- neues Raster: Teilspalten, Zugabe 100 --

def test_neues_raster_16_aufgaben_zehn_plaetze() -> None:
    # k = 4 Teilspalten zu je 4 Aufgaben: Mappe 4 * 2100 + 3 * 150 = 8850 breit, 150 + 4 * 800 + 3 * 150 = 3800 hoch.
    r = gruppen_raster([800] * 16)
    assert (r.teilspalten, r.zugabe) == (4, 100)
    assert (r.breite, r.hoehe) == (8850, 3800)
    assert (r.spalten, r.zeilen) == (19_800 // 8950, 19_800 // 3900) == (2, 5)
    assert r.plaetze == gruppen_kapazitaet([800] * 16) == 10


def test_neues_raster_17_aufgaben_neun_plaetze() -> None:
    # Eine Spalte (2200 x 16 250 je Platz): 9 x 1. Mehr Teilspalten bringen hoechstens 8.
    r = gruppen_raster([800] * 17)
    assert (r.teilspalten, r.breite, r.hoehe, r.spalten, r.zeilen) == (1, 2100, 16_150, 9, 1)
    assert gruppen_kapazitaet([800] * 17) == 9


def _plaetze_bei(h: list[int], k: int) -> int:
    """Nachrechnung ohne das Modul: k Teilspalten, gleichmaessig Hoehe der hoechsten (bei gleichen Hoehen)."""
    je = -(-len(h) // k)
    spalten_genutzt = -(-len(h) // je)
    breite = spalten_genutzt * 2100 + (spalten_genutzt - 1) * 150
    hoehe = 150 + je * h[0] + (je - 1) * 150
    return (19_800 // (breite + 100)) * (19_800 // (hoehe + 100))


@pytest.mark.parametrize("n", [1, 3, 5, 8, 12, 16, 17, 20, 24, 30])
def test_kapazitaet_gleich_hoher_aufgaben_nachgerechnet(n: int) -> None:
    h = [800] * n
    assert gruppen_kapazitaet(h) == max(_plaetze_bei(h, k) for k in range(1, n + 1))


def test_gleichstand_nimmt_das_kleinere_k() -> None:
    # 3 Aufgaben: k = 1 (2200 x 2950 -> 9 x 6 = 54) schlaegt alle anderen; bei Gleichstand bleibt das kleinste k.
    for h in ([800] * 3, [800] * 17, [800, 1100] * 8):
        r = gruppen_raster(h)
        kleinere = [k for k in range(1, r.teilspalten)]
        assert all(gruppen_raster_k(h, k).plaetze < r.plaetze for k in kleinere)


def gruppen_raster_k(h: list[int], k: int) -> GruppenRaster:
    from spaces.learning.huelle.anordnung import _neues_raster

    return _neues_raster(h, k)


def test_teilspalten_reihenfolge_und_abstand() -> None:
    r = gruppen_raster([800] * 16)
    bereiche = mappen_bereiche([800] * 16, (100, 100), r)
    # Spaltenweise: Aufgaben 1-4 in Teilspalte 1, 5-8 in Teilspalte 2 ... (x = 100 + j * 2250).
    assert [b["x"] for b in bereiche] == [100 + (i // 4) * 2250 for i in range(16)]
    assert [b["y"] for b in bereiche[:4]] == [250, 1200, 2150, 3100]
    assert bereiche[4]["y"] == 250
    # Zwischen zwei Teilspalten (ganze Zeilen samt Kartenfeld) liegen genau 150.
    assert bereiche[4]["x"] - (bereiche[0]["x"] + 2100) == 150


def test_teilspalten_gemischt_hoechste_spalte_moeglichst_niedrig() -> None:
    from spaces.learning.huelle.anordnung import _teilspalten

    h = [1100, 800, 800, 1100, 800, 800]
    bereiche = _teilspalten(h, 2)
    spalten = itertools.groupby(bereiche, key=lambda b: b["x"])
    hoehen_je = [sum(b["h"] for b in g) for _, g in spalten]
    assert len(hoehen_je) == 2 and max(hoehen_je) == 2700  # 1100 + 800 + 800 | 1100 + 800 + 800
    assert _teilspalten([], 3) == []


_FAELLE = [[800] * n for n in (1, 2, 3, 5, 8, 12, 16, 17, 20, 25, 30, 40)] + [
    [1100] * 8, [1100] * 16, [800, 1100] * 8, [800] * 12 + [1100] * 4, [1100] + [800] * 15, [800] * 15 + [1100]]


@pytest.mark.parametrize("h", _FAELLE, ids=lambda h: f"{len(h)}-{sum(h)}")
def test_neue_mappen_ueberlappen_nicht_und_bleiben_auf_der_flaeche(h: list[int]) -> None:
    """Fuer jeden Platz: alle Zeilen (Bereich + Kartenfeld), Kopf und Abschlusskarte liegen in der eigenen Zelle,
    nichts ueberlappt (weder zwischen Mappen noch zwischen Teilspalten), alles innerhalb von 0 ... 20 000."""
    from spaces.learning.huelle.penecho_mcp import felder

    r = gruppen_raster(h)
    alle: list[tuple[int, dict[str, int]]] = []
    for platz in range(r.plaetze):
        u = mappen_ursprung(platz, r)
        zelle = {"x": u[0], "y": u[1], "w": r.breite, "h": r.hoehe}
        bereiche = mappen_bereiche(h, u, r)
        assert len(bereiche) == len(h)
        stuecke = [zeile(b) for b in bereiche] + [mappen_kopf(u), mappen_abschluss(u)]
        for s in stuecke:
            assert zelle["x"] <= s["x"] and s["x"] + s["w"] <= zelle["x"] + zelle["w"], (platz, s)
            assert zelle["y"] <= s["y"] and s["y"] + s["h"] <= zelle["y"] + zelle["h"], (platz, s)
            assert 0 <= s["x"] and s["x"] + s["w"] <= 20_000 and 0 <= s["y"] and s["y"] + s["h"] <= 20_000
        for a, b in itertools.combinations(stuecke, 2):
            assert _getrennt(a, b), (platz, a, b)
        # Text, Schutzfelder (Ergebnis- bzw. Arbeitsfeld, `loesungsfeld`) und Karte liegen in der Zeile ihrer Aufgabe.
        for b in bereiche:
            f = felder(b)
            z = zeile(b)
            for feld in (f.textfeld, f.ergebnisfeld, f.arbeitsfeld, f.rueckmeldungsfeld):
                assert z["x"] <= feld["x"] and feld["x"] + feld["w"] <= z["x"] + z["w"]
                assert z["y"] <= feld["y"] and feld["y"] + feld["h"] <= z["y"] + z["h"]
        alle += [(platz, zelle)]
    # Die Zellen zweier Mappen liegen mindestens 100 auseinander.
    for (_, a), (_, b) in itertools.combinations(alle, 2):
        assert _getrennt({**a, "w": a["w"] + 100, "h": a["h"] + 100}, b) or _getrennt(a, {**b, "w": b["w"] + 100,
                                                                                          "h": b["h"] + 100})


def test_spaeter_kommende_mappe_im_festen_neuen_raster() -> None:
    fest = gruppen_raster([800] * 16)  # k = 4, 3800 hoch
    assert passt_in([800] * 16, fest) and passt_in([800] * 12, fest)
    assert not passt_in([800] * 17, fest)  # fuenf Aufgaben je Teilspalte: zu hoch
    assert not passt_in([1100] * 16, fest)
    # Eine kleinere Mappe rechnet in den Teilspalten des festen Rasters und bleibt in ihrer Zelle.
    u = mappen_ursprung(9, fest)
    bereiche = mappen_bereiche([800] * 12, u, fest)
    assert {b["x"] - u[0] for b in bereiche} == {0, 2250, 4500, 6750}
    assert max(b["y"] + b["h"] for b in bereiche) <= u[1] + fest.hoehe


def test_kapazitaet_null_wenn_nichts_passt() -> None:
    # Eine einzelne Aufgabe ist nie hoeher als die Flaeche; sehr viele grosse Aufgaben passen nicht mehr.
    assert gruppen_kapazitaet([1100] * 200) == 0
    with pytest.raises(ZuVieleAufgaben):
        gruppen_raster([1100] * 200)
    assert gruppen_kapazitaet([]) >= 1
