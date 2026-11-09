"""Anordnung der Aufgabenbereiche auf der Lernflaeche (rein, ohne Abhaengigkeiten)."""

from __future__ import annotations

import json
from collections.abc import Sequence

BREITE = 1300
HOEHE = 800
HOEHE_GROSS = 1100
LUECKE = 150
SPALTEN_X = (100, 3000)
OBEN = 100
UNTEN = 19_900
ZEILEN_BREITE = 1760


class ZuVieleAufgaben(ValueError):
    """Eine Aufgabe passt nicht mehr auf die Flaeche."""


def anordnung(gross: Sequence[bool]) -> list[dict[str, int]]:
    """Rechtecke von oben nach unten in Spalte 1, dann in Spalte 2."""
    bereiche: list[dict[str, int]] = []
    spalte = 0
    y = OBEN
    for ist_gross in gross:
        h = HOEHE_GROSS if ist_gross else HOEHE
        if y + h > UNTEN:
            spalte += 1
            y = OBEN
            if spalte >= len(SPALTEN_X) or y + h > UNTEN:
                raise ZuVieleAufgaben(f"Aufgabe {len(bereiche) + 1} passt nicht mehr auf die Flaeche")
        bereiche.append({"x": SPALTEN_X[spalte], "y": y, "w": BREITE, "h": h})
        y += h + LUECKE
    return bereiche


def zeile(bereich: dict[str, int]) -> dict[str, int]:
    return {"x": bereich["x"], "y": bereich["y"], "w": ZEILEN_BREITE, "h": bereich["h"]}


def markenzeile(bereiche: Sequence[dict[str, int]]) -> str:
    rechtecke = [[z["x"], z["y"], z["w"], z["h"]] for z in map(zeile, bereiche)]
    return f"<!-- lernflaeche-bereiche: {json.dumps(rechtecke, separators=(',', ':'))} -->"
