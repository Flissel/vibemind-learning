"""Anordnung der Aufgabenbereiche auf der Lernflaeche (rein, ohne Abhaengigkeiten).

Teil C (Gruppenflaeche): jede Person hat eine Mappe. Eine Mappe ist ein Kopf (Namensschild) ueber den Aufgabenzeilen
wie im Solo; die Mappen liegen zeilenweise in einem Platzraster auf der 20 000er-Flaeche (Plan P3).

Kapazitaet (12.11.): neue Gruppen verteilen die Aufgaben einer Mappe auf k Teilspalten (Abstand 150) und lassen
100 Luft je Richtung; k ist so gewaehlt, dass die meisten Mappen auf die Flaeche passen. Gruppen, deren Raster schon
fest ist (`gruppen-gesetzt.json` ohne `teilspalten`), rechnen weiter im alten Raster: eine Spalte, ab
`MAPPE_HOECHSTENS` zwei im Abstand 2900, 200 Luft. Laufende Gruppen verschieben sich so nie.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

BREITE = 1300
HOEHE = 800
HOEHE_GROSS = 1100
LUECKE = 150
SPALTEN_X = (100, 3000)
OBEN = 100
UNTEN = 19_900
# Bereich + Abstand + Kartenfeld (penecho_mcp.felder): 1300 + 40 + 760. Die Aufgabenwahl rahmt die ganze Zeile.
ZEILEN_BREITE = 2100


# Teil C: Mappen der Gruppenflaeche.
KOPF = 150  # Kopf der Mappe (Namensschild) ueber der ersten Aufgabenzeile
MAPPE_HOECHSTENS = 19_600  # hoeher wird eine Spalte (mit Kopf) nie; sonst zwei Spalten
ZWEI_SPALTEN_BREITE = SPALTEN_X[1] - SPALTEN_X[0] + ZEILEN_BREITE  # 2900 Abstand + 2100 = 5000
PLATZ_ZUGABE = 200  # Luft zwischen zwei Mappen, je Richtung (altes Raster)
NUTZFLAECHE = 19_800  # 20 000 abzueglich je 100 Rand
_SCHILD_RAND = 20
# Neues Raster: Teilspalten einer Mappe im Abstand LUECKE, also je Zeilenbreite + 150; weniger Luft zwischen Mappen.
TEILSPALTEN_SCHRITT = ZEILEN_BREITE + LUECKE  # 2250
ZUGABE_NEU = 100


class ZuVieleAufgaben(ValueError):
    """Eine Aufgabe passt nicht mehr auf die Flaeche."""


class MappeVoll(ValueError):
    """Der Platz liegt ausserhalb des Platzrasters: keine Mappe passt mehr auf die Gruppenflaeche."""


def anordnung(gross: Sequence[bool]) -> list[dict[str, int]]:
    """Rechtecke von oben nach unten in Spalte 1, dann in Spalte 2."""
    return _spalten(hoehen(gross), UNTEN)


def hoehen(gross: Sequence[bool]) -> list[int]:
    return [HOEHE_GROSS if ist_gross else HOEHE for ist_gross in gross]


def _spalten(hoehen_: Sequence[int], unten: int) -> list[dict[str, int]]:
    bereiche: list[dict[str, int]] = []
    spalte = 0
    y = OBEN
    for h in hoehen_:
        if y + h > unten:
            spalte += 1
            y = OBEN
            if spalte >= len(SPALTEN_X) or y + h > unten:
                raise ZuVieleAufgaben(f"Aufgabe {len(bereiche) + 1} passt nicht mehr auf die Flaeche")
        bereiche.append({"x": SPALTEN_X[spalte], "y": y, "w": BREITE, "h": h})
        y += h + LUECKE
    return bereiche


# ---- Teil C: Mappen auf der Gruppenflaeche ------------------------------------------------------------

@dataclass(frozen=True)
class GruppenRaster:
    """Groesse einer Mappe und das Platzraster der Gruppenflaeche (Plaetze zeilenweise).

    `teilspalten`: Zahl der Teilspalten einer Mappe im neuen Raster; None ist das alte Raster (eine Spalte, ab
    `MAPPE_HOECHSTENS` zwei im Abstand 2900). `zugabe`: Luft je Richtung zwischen zwei Mappen. Die Vorgaben sind
    das alte Raster, wie es in `gruppen-gesetzt.json` mit vier Zahlen steht.
    """

    breite: int
    hoehe: int
    spalten: int
    zeilen: int
    teilspalten: int | None = None
    zugabe: int = PLATZ_ZUGABE

    @property
    def platzbreite(self) -> int:
        return self.breite + self.zugabe

    @property
    def platzhoehe(self) -> int:
        return self.hoehe + self.zugabe

    @property
    def plaetze(self) -> int:
        return self.spalten * self.zeilen


def _mappe(hoehen_: Sequence[int]) -> list[dict[str, int]]:
    """Solo-Bereiche einer Mappe (Ursprung wie im Solo bei (100, 100)), Spaltenbruch so, dass Kopf plus Spalte
    hoechstens MAPPE_HOECHSTENS hoch sind. Passt alles in eine Spalte, bleibt es eine. (Altes Raster.)"""
    return _spalten(hoehen_, OBEN + MAPPE_HOECHSTENS - KOPF)


def _altes_raster(hoehen_: Sequence[int]) -> GruppenRaster:
    """Altes Raster: Kopf 150 ueber den Aufgabenzeilen (Breite 2100, Abstand 150); zwei Spalten im Abstand 2900
    (Breite 5000) nur, wenn eine Spalte samt Kopf hoeher als 19 600 wuerde. Platz = Mappe + 200 je Richtung."""
    bereiche = _mappe(hoehen_)
    zwei = any(b["x"] != SPALTEN_X[0] for b in bereiche)
    breite = ZWEI_SPALTEN_BREITE if zwei else ZEILEN_BREITE
    hoehe = KOPF + max((b["y"] + b["h"] - OBEN for b in bereiche), default=0)
    return GruppenRaster(breite, hoehe, NUTZFLAECHE // (breite + PLATZ_ZUGABE),
                         NUTZFLAECHE // (hoehe + PLATZ_ZUGABE))


def _fuellen(hoehen_: Sequence[int], grenze: int) -> list[dict[str, int]]:
    """Aufgaben der Reihe nach von oben in Teilspalten (x = 100 + j * 2250); eine Teilspalte ist hoechstens
    `grenze` hoch, ausser eine einzelne Aufgabe ist schon hoeher (dann steht sie allein)."""
    bereiche: list[dict[str, int]] = []
    spalte, y = 0, OBEN
    for h in hoehen_:
        if y > OBEN and y + h > OBEN + grenze:
            spalte, y = spalte + 1, OBEN
        bereiche.append({"x": OBEN + spalte * TEILSPALTEN_SCHRITT, "y": y, "w": BREITE, "h": h})
        y += h + LUECKE
    return bereiche


def _teilspalten_zahl(bereiche: Sequence[dict[str, int]]) -> int:
    return len({b["x"] for b in bereiche})


def _teilspalten(hoehen_: Sequence[int], k: int) -> list[dict[str, int]]:
    """Solo-Bereiche einer Mappe (Ursprung (100, 100)) auf hoechstens k Teilspalten, Reihenfolge wie die Liste.

    Die hoechste Teilspalte ist so niedrig wie moeglich: die kleinste Spaltenhoehe (Summe einer zusammenhaengenden
    Folge plus Abstaende), mit der das Fuellen von oben hoechstens k Teilspalten braucht.
    """
    if k < 1:
        raise ValueError("teilspalten")
    if not hoehen_:
        return []
    n = len(hoehen_)
    kandidaten = sorted({sum(hoehen_[i:j]) + LUECKE * (j - i - 1) for i in range(n) for j in range(i + 1, n + 1)})
    unten, oben = 0, len(kandidaten) - 1  # die groesste (alles in einer Spalte) reicht immer
    while unten < oben:
        mitte = (unten + oben) // 2
        if _teilspalten_zahl(_fuellen(hoehen_, kandidaten[mitte])) <= k:
            oben = mitte
        else:
            unten = mitte + 1
    return _fuellen(hoehen_, kandidaten[unten])


def _masse(bereiche: Sequence[dict[str, int]]) -> tuple[int, int]:
    """(Breite, Hoehe) einer Mappe mit Kopf aus ihren Solo-Bereichen."""
    teilspalten = max(_teilspalten_zahl(bereiche), 1)
    breite = teilspalten * TEILSPALTEN_SCHRITT - LUECKE
    return breite, KOPF + max((b["y"] + b["h"] - OBEN for b in bereiche), default=0)


def _neues_raster(hoehen_: Sequence[int], k: int) -> GruppenRaster:
    breite, hoehe = _masse(_teilspalten(hoehen_, k))
    return GruppenRaster(breite, hoehe, NUTZFLAECHE // (breite + ZUGABE_NEU), NUTZFLAECHE // (hoehe + ZUGABE_NEU),
                         teilspalten=k, zugabe=ZUGABE_NEU)


def gruppen_raster(hoehen_: Sequence[int]) -> GruppenRaster:
    """Raster einer neuen Gruppe: die Zahl k der Teilspalten (1 bis Aufgabenzahl) mit den meisten Plaetzen
    (`19 800 // Platzbreite * 19 800 // Platzhoehe`, Platz = Mappe + 100 je Richtung), bei Gleichstand das kleinere
    k. `ZuVieleAufgaben`, wenn nicht einmal eine Mappe auf die Flaeche passt."""
    beste: GruppenRaster | None = None
    for k in range(1, max(len(hoehen_), 1) + 1):
        raster = _neues_raster(hoehen_, k)
        if beste is None or raster.plaetze > beste.plaetze:
            beste = raster
    assert beste is not None
    if beste.plaetze == 0:
        raise ZuVieleAufgaben("Die Mappe passt nicht auf die Gruppenflaeche")
    return beste


def gruppen_kapazitaet(hoehen_: Sequence[int]) -> int:
    """Wie viele Mappen dieser Aufgabenhoehen eine neue Gruppenflaeche fasst (0: nicht einmal eine)."""
    try:
        return gruppen_raster(hoehen_).plaetze
    except ZuVieleAufgaben:
        return 0


def _bereiche_im_raster(hoehen_: Sequence[int], raster: GruppenRaster | None) -> list[dict[str, int]]:
    if raster is None or raster.teilspalten is None:
        return _mappe(hoehen_)
    return _teilspalten(hoehen_, raster.teilspalten)


def passt_in(hoehen_: Sequence[int], raster: GruppenRaster) -> bool:
    """Ob eine Mappe dieser Aufgabenhoehen in die Zelle des (festen) Rasters passt. Altes Raster wie bisher: das
    alte Raster der Mappe ist hoechstens so breit und so hoch (`ZuVieleAufgaben` ab drei Spalten)."""
    if raster.teilspalten is None:
        eigen = _altes_raster(hoehen_)
        breite, hoehe = eigen.breite, eigen.hoehe
    else:
        breite, hoehe = _masse(_teilspalten(hoehen_, raster.teilspalten))
    return breite <= raster.breite and hoehe <= raster.hoehe


def mappen_ursprung(platz: int, raster: GruppenRaster) -> tuple[int, int]:
    """Linke obere Ecke der Mappe auf `platz` (zeilenweise); `MappeVoll` ausserhalb des Rasters."""
    if isinstance(platz, bool) or not isinstance(platz, int) or not 0 <= platz < raster.spalten * raster.zeilen:
        raise MappeVoll(f"Platz {platz} passt nicht auf die Gruppenflaeche")
    return (OBEN + (platz % raster.spalten) * raster.platzbreite,
            OBEN + (platz // raster.spalten) * raster.platzhoehe)


def mappen_bereiche(
    hoehen_: Sequence[int], ursprung: tuple[int, int], raster: GruppenRaster | None = None,
) -> list[dict[str, int]]:
    """Die Bereiche einer Mappe: die Solo-Bereiche, verschoben um den Ursprung plus den Kopf (0, 150).

    Der Solo-Rand 100 steckt schon im Ursprung (Pre-flight-Ruling): Bereich = (ux + x - 100, uy + 150 + y - 100).
    Die Solo-Bereiche kommen aus dem Raster: im neuen auf dessen Teilspalten, im alten (oder ohne Raster) wie bisher.
    """
    ux, uy = ursprung
    return [{**b, "x": ux + b["x"] - OBEN, "y": uy + KOPF + b["y"] - OBEN}
            for b in _bereiche_im_raster(hoehen_, raster)]


def mappen_kopf(ursprung: tuple[int, int]) -> dict[str, int]:
    """Platz des Namensschilds im Kopf der Mappe, links oben, ganz innerhalb der 150 Kopfhoehe."""
    ux, uy = ursprung
    return {"x": ux + _SCHILD_RAND, "y": uy + _SCHILD_RAND, "w": BREITE, "h": KOPF - 2 * _SCHILD_RAND}


def mappen_abschluss(ursprung: tuple[int, int]) -> dict[str, int]:
    """Fester Platz der Abschlusskarte (Final B, H3): im Kopf der Mappe rechts neben dem Namensschild, bis zum
    rechten Rand der Aufgabenzeile (740 x 110). Liegt so immer in der eigenen Mappe, nie auf Schild oder Aufgabe."""
    kopf = mappen_kopf(ursprung)
    x = kopf["x"] + kopf["w"] + _SCHILD_RAND
    return {"x": x, "y": kopf["y"], "w": ursprung[0] + ZEILEN_BREITE - _SCHILD_RAND - x, "h": kopf["h"]}


def zeile(bereich: dict[str, int]) -> dict[str, int]:
    return {"x": bereich["x"], "y": bereich["y"], "w": ZEILEN_BREITE, "h": bereich["h"]}


def markenzeile(bereiche: Sequence[dict[str, int]]) -> str:
    rechtecke = [[z["x"], z["y"], z["w"], z["h"]] for z in map(zeile, bereiche)]
    return f"<!-- lernflaeche-bereiche: {json.dumps(rechtecke, separators=(',', ':'))} -->"
