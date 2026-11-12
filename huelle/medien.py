"""Bildfelder der Aufgaben und die Hoehenregel ihrer Bereiche (rein, ohne Abhaengigkeiten).

Gemeinsam fuer die Huelle (`bruecke`) und learning-mcp (`learning.group.capacity`): beide rechnen die Hoehe einer
Aufgabe aus derselben Aufgabe (`PublicTask` wie in `learning.task.list`) mit derselben Regel, damit die Kapazitaet
einer Gruppenflaeche genau die Mappen zaehlt, die die Huelle setzt.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from typing import Any

from .anordnung import hoehen

# Bilder der Aufgaben (gezeichnete Graphen, Terminalfenster) liegen bei learning-api; die Seite holt sie
# ueber die eigene Route. Gleiches Namensmuster wie der Fork (practice.py `_MEDIA_PATH`).
MEDIEN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(png|jpg|webp)$")
MEDIEN_PFAD = re.compile(r"^/lernsequenz-medien/([A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:png|jpg|webp))$")
MEDIEN_ROUTE = "/lernen/api/medien/"


def medien(turn: dict[str, Any]) -> dict[str, Any]:
    """Bildfelder der Aufgabe auf die eigene Route umschreiben, alles andere darin entfernen (wie der Fork)."""
    task = turn.get("task")
    presentation = task.get("presentation") if isinstance(task, dict) else None
    if not isinstance(presentation, dict):
        return turn

    def fix(eintrag: dict[str, Any]) -> None:
        if "image" not in eintrag or eintrag["image"] is None:
            return
        bild = eintrag["image"]
        treffer = MEDIEN_PFAD.fullmatch(bild) if isinstance(bild, str) else None
        if treffer:
            eintrag["image"] = MEDIEN_ROUTE + treffer.group(1)
        else:
            eintrag.pop("image")

    fix(presentation)
    for seite in ("left", "right"):
        eintraege = presentation.get(seite)
        for eintrag in eintraege if isinstance(eintraege, list) else []:
            if isinstance(eintrag, dict):
                fix(eintrag)
    return turn


def bildname(wert: object) -> str | None:
    """Name eines Aufgabenbilds auf der eigenen Route (nach `medien`); sonst None."""
    if not isinstance(wert, str) or not wert.startswith(MEDIEN_ROUTE):
        return None
    name = wert.removeprefix(MEDIEN_ROUTE)
    return name if MEDIEN_NAME.fullmatch(name) else None


def hat_bilder(task: dict[str, Any]) -> bool:
    """Ob die Aufgabe (nach `medien`) Bilder hat, aus denselben Eintraegen wie `Bruecke._aufgabenbilder`."""
    presentation = task.get("presentation")
    if not isinstance(presentation, dict):
        return False
    eintraege = [presentation]
    for seite in ("left", "right"):
        liste = presentation.get(seite)
        eintraege += [e for e in (liste if isinstance(liste, list) else []) if isinstance(e, dict)]
    return any(bildname(e.get("image")) is not None for e in eintraege)


def gross(task: dict[str, Any]) -> bool:
    """Hoher Bereich (anordnung.HOEHE_GROSS): Aufgaben mit Bildern und Zeichenaufgaben (Aufgabe nach `medien`)."""
    return task.get("type") == "penecho_canvas" or hat_bilder(task)


def aufgaben_hoehen(aufgaben: Iterable[dict[str, Any]]) -> list[int]:
    """Bereichshoehen der Aufgaben, wie Learning sie liefert (vor `medien`), in Listenreihenfolge.

    Wie die Huelle: erst die Bildfelder umschreiben (`_listeneintraege`), dann `gross`. Die Eingabe bleibt
    unveraendert."""
    return hoehen([gross(medien({"task": copy.deepcopy(task)})["task"]) for task in aufgaben])
