"""Vorgedruckte Vorlagen im Arbeitsfeld: Matrix und Onepager als Text plus Tinte.

Nur Geometrie und Beschriftung, kein PenEcho-Aufruf (das macht `PenEchoMcp.aufgabe_setzen`). Alles ist
Text (create_text) und Tinte (draw_ink), also keine Widgets: die fingen Stifteingaben ab. Die Werte
`VORLAGEN`, `STANDARD_FELDER`, `FELDER_ANZAHL` und `FELD_MAX` entsprechen denen des Learning-Dienstes
(`darstellung`); die Huelle importiert ihn nicht, test_vorlage_etiketten haelt beide gleich.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

VORLAGEN = ("matrix", "onepager")

# Matrix: oben links, oben rechts, unten links, unten rechts (Nutzen nach oben, Aufwand nach rechts).
STANDARD_FELDER: dict[str, tuple[str, ...]] = {
    "matrix": ("Quick Wins", "Große Vorhaben", "Nebenbei", "Lieber nicht"),
    "onepager": ("Ausgangslage", "Empfohlene Use Cases", "Nutzen", "Aufwand", "Risiken", "Nächster Schritt"),
}
FELDER_ANZAHL: dict[str, tuple[int, int]] = {"matrix": (4, 4), "onepager": (3, 6)}
FELD_MAX = 40

_RAND = 40  # Innenrand der Matrix
_ABSTAND = 10  # Abstand der Feldnamen und Ueberschriften
_ZEILE = 40  # Texthoehe
_ACHSE_BREITE = 300  # Breite der Achsenbeschriftung
_MATRIX_PLAETZE = ((0, 0), (1, 0), (0, 1), (1, 1))  # (Spalte, Zeile) der vier Feldnamen


class Vorlagenplan(NamedTuple):
    """Texte (Wortlaut, Region) und Striche (Punktfolgen) einer Vorlage, alle innerhalb des Feldes."""

    texte: list[tuple[str, dict[str, int]]]
    striche: list[list[dict[str, int]]]


def _linie(x1: int, y1: int, x2: int, y2: int) -> list[dict[str, int]]:
    return [{"x": x1, "y": y1}, {"x": x2, "y": y2}]


def _namen(art: str, felder: Sequence[str] | None) -> tuple[str, ...]:
    """Die eigenen Feldnamen, wenn Anzahl und Laenge passen; sonst die Standardnamen."""
    unten, oben = FELDER_ANZAHL[art]
    if felder is not None and unten <= len(felder) <= oben and all(
            isinstance(f, str) and 0 < len(f.strip()) <= FELD_MAX for f in felder):
        return tuple(f.strip() for f in felder)
    return STANDARD_FELDER[art]


def _matrix(feld: dict[str, int], namen: tuple[str, ...]) -> Vorlagenplan:
    ix, iy = feld["x"] + _RAND, feld["y"] + _RAND
    iw, ih = feld["w"] - 2 * _RAND, feld["h"] - 2 * _RAND
    qw, qh = iw // 2, ih // 2
    breite = min(_ACHSE_BREITE, iw)
    texte = [
        ("Nutzen ↑", {"x": ix, "y": iy - _ZEILE, "w": breite, "h": _ZEILE}),
        ("Aufwand →", {"x": ix + iw - breite, "y": iy + ih, "w": breite, "h": _ZEILE}),
    ]
    for name, (spalte, zeile) in zip(namen, _MATRIX_PLAETZE):
        texte.append((name, {"x": ix + spalte * qw + _ABSTAND, "y": iy + zeile * qh + _ABSTAND,
                             "w": qw - 2 * _ABSTAND, "h": _ZEILE}))
    striche = [
        _linie(ix, iy, ix, iy + ih),  # y-Achse (Nutzen), von oben nach unten
        _linie(ix, iy + ih, ix + iw, iy + ih),  # x-Achse (Aufwand), von links nach rechts
        _linie(ix + qw, iy, ix + qw, iy + ih),  # senkrechte Teilung
        _linie(ix, iy + qh, ix + iw, iy + qh),  # waagrechte Teilung
    ]
    return Vorlagenplan(texte, striche)


def _onepager(feld: dict[str, int], namen: tuple[str, ...]) -> Vorlagenplan:
    hoehe = feld["h"] // len(namen)
    texte: list[tuple[str, dict[str, int]]] = []
    striche: list[list[dict[str, int]]] = []
    for i, name in enumerate(namen):
        oben = feld["y"] + i * hoehe
        texte.append((f"{i + 1}. {name}", {"x": feld["x"] + _ABSTAND, "y": oben + _ABSTAND,
                                           "w": feld["w"] - 2 * _ABSTAND, "h": _ZEILE}))
        if i < len(namen) - 1:
            striche.append(_linie(feld["x"], oben + hoehe, feld["x"] + feld["w"], oben + hoehe))
    return Vorlagenplan(texte, striche)


def vorlagenplan(art: str, feld: dict[str, int], *, felder: Sequence[str] | None = None) -> Vorlagenplan | None:
    """Plan der Vorlage `art` im Feld `feld`; eine unbekannte Art ergibt None.

    `felder`: eigene Feldnamen der Aufgabe (Matrix 4, Onepager 3-6, je hoechstens FELD_MAX Zeichen);
    passen sie nicht, druckt die Vorlage die Standardnamen.
    """
    if art == "matrix":
        return _matrix(feld, _namen(art, felder))
    if art == "onepager":
        return _onepager(feld, _namen(art, felder))
    return None


def vorlagenfeld(arbeitsfeld: dict[str, int], *, mit_bildern: bool) -> dict[str, int]:
    """Das Feld, in das die Vorlage kommt: das Arbeitsfeld, mit Aufgabenbildern dessen untere Hälfte.

    `bildfeld` belegt bei Aufgaben mit Bildern die obere Hälfte des Arbeitsfeldes.
    """
    a = arbeitsfeld
    if not mit_bildern:
        return dict(a)
    y = a["y"] + a["h"] // 2 + 20
    return {"x": a["x"], "y": y, "w": a["w"], "h": a["y"] + a["h"] - y}
