"""Anordnung der Aufgabenbereiche auf der Lernflaeche (rein, ohne Abhaengigkeiten).

Teil C (Gruppenflaeche): jede Person hat eine Mappe. Eine Mappe ist ein Kopf (Namensschild) ueber den Aufgabenzeilen
wie im Solo; die Mappen liegen zeilenweise in einem Platzraster auf der 20 000er-Flaeche (Plan P3).
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
PLATZ_ZUGABE = 200  # Luft zwischen zwei Mappen, je Richtung
NUTZFLAECHE = 19_800  # 20 000 abzueglich je 100 Rand
_SCHILD_RAND = 20


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
    """Groesse einer Mappe und das Platzraster der Gruppenflaeche (Plaetze zeilenweise)."""

    breite: int
    hoehe: int
    spalten: int
    zeilen: int

    @property
    def platzbreite(self) -> int:
        return self.breite + PLATZ_ZUGABE

    @property
    def platzhoehe(self) -> int:
        return self.hoehe + PLATZ_ZUGABE


def _mappe(hoehen_: Sequence[int]) -> list[dict[str, int]]:
    """Solo-Bereiche einer Mappe (Ursprung wie im Solo bei (100, 100)), Spaltenbruch so, dass Kopf plus Spalte
    hoechstens MAPPE_HOECHSTENS hoch sind. Passt alles in eine Spalte, bleibt es eine."""
    return _spalten(hoehen_, OBEN + MAPPE_HOECHSTENS - KOPF)


def gruppen_raster(hoehen_: Sequence[int]) -> GruppenRaster:
    """Mappe: Kopf 150 ueber den Aufgabenzeilen (Breite 2100, Abstand 150); zwei Spalten im Abstand 2900 (Breite
    5000) nur, wenn eine Spalte samt Kopf hoeher als 19 600 wuerde. Platz = Mappe + 200 je Richtung."""
    bereiche = _mappe(hoehen_)
    zwei = any(b["x"] != SPALTEN_X[0] for b in bereiche)
    breite = ZWEI_SPALTEN_BREITE if zwei else ZEILEN_BREITE
    hoehe = KOPF + max((b["y"] + b["h"] - OBEN for b in bereiche), default=0)
    return GruppenRaster(breite, hoehe, NUTZFLAECHE // (breite + PLATZ_ZUGABE),
                         NUTZFLAECHE // (hoehe + PLATZ_ZUGABE))


def mappen_ursprung(platz: int, raster: GruppenRaster) -> tuple[int, int]:
    """Linke obere Ecke der Mappe auf `platz` (zeilenweise); `MappeVoll` ausserhalb des Rasters."""
    if isinstance(platz, bool) or not isinstance(platz, int) or not 0 <= platz < raster.spalten * raster.zeilen:
        raise MappeVoll(f"Platz {platz} passt nicht auf die Gruppenflaeche")
    return (OBEN + (platz % raster.spalten) * raster.platzbreite,
            OBEN + (platz // raster.spalten) * raster.platzhoehe)


def mappen_bereiche(hoehen_: Sequence[int], ursprung: tuple[int, int]) -> list[dict[str, int]]:
    """Die Bereiche einer Mappe: die Solo-Bereiche, verschoben um den Ursprung plus den Kopf (0, 150).

    Der Solo-Rand 100 steckt schon im Ursprung (Pre-flight-Ruling): Bereich = (ux + x - 100, uy + 150 + y - 100).
    """
    ux, uy = ursprung
    return [{**b, "x": ux + b["x"] - OBEN, "y": uy + KOPF + b["y"] - OBEN} for b in _mappe(hoehen_)]


def mappen_kopf(ursprung: tuple[int, int]) -> dict[str, int]:
    """Platz des Namensschilds im Kopf der Mappe, links oben, ganz innerhalb der 150 Kopfhoehe."""
    ux, uy = ursprung
    return {"x": ux + _SCHILD_RAND, "y": uy + _SCHILD_RAND, "w": BREITE, "h": KOPF - 2 * _SCHILD_RAND}


def zeile(bereich: dict[str, int]) -> dict[str, int]:
    return {"x": bereich["x"], "y": bereich["y"], "w": ZEILEN_BREITE, "h": bereich["h"]}


def markenzeile(bereiche: Sequence[dict[str, int]]) -> str:
    rechtecke = [[z["x"], z["y"], z["w"], z["h"]] for z in map(zeile, bereiche)]
    return f"<!-- lernflaeche-bereiche: {json.dumps(rechtecke, separators=(',', ':'))} -->"
