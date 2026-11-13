"""Vorgedruckte Vorlagen im Arbeitsfeld: Matrix und Onepager als Text plus Tinte.

Nur Geometrie und Beschriftung, kein PenEcho-Aufruf (das macht `PenEchoMcp.aufgabe_setzen`). Alles ist
Text (create_text) und Tinte (draw_ink), also keine Widgets: die fingen Stifteingaben ab. Die Werte
`VORLAGEN` entsprechen denen des Learning-Dienstes (`darstellung.VORLAGEN`); die Huelle importiert ihn nicht.
"""

from __future__ import annotations

from typing import NamedTuple

VORLAGEN = ("matrix", "onepager")

_RAND = 40  # Innenrand der Matrix
_ABSTAND = 10  # Abstand der Feldnamen und Ueberschriften
_ZEILE = 40  # Texthoehe
_ACHSE_BREITE = 300  # Breite der Achsenbeschriftung

_ABSCHNITTE = ("1. Ausgangslage", "2. Empfohlene Use Cases", "3. Nutzen", "4. Aufwand", "5. Risiken",
               "6. Nächster Schritt")


class Vorlagenplan(NamedTuple):
    """Texte (Wortlaut, Region) und Striche (Punktfolgen) einer Vorlage, alle innerhalb des Feldes."""

    texte: list[tuple[str, dict[str, int]]]
    striche: list[list[dict[str, int]]]


def _linie(x1: int, y1: int, x2: int, y2: int) -> list[dict[str, int]]:
    return [{"x": x1, "y": y1}, {"x": x2, "y": y2}]


def _matrix(feld: dict[str, int]) -> Vorlagenplan:
    ix, iy = feld["x"] + _RAND, feld["y"] + _RAND
    iw, ih = feld["w"] - 2 * _RAND, feld["h"] - 2 * _RAND
    qw, qh = iw // 2, ih // 2
    breite = min(_ACHSE_BREITE, iw)
    texte = [
        ("Nutzen ↑", {"x": ix, "y": iy - _ZEILE, "w": breite, "h": _ZEILE}),
        ("Aufwand →", {"x": ix + iw - breite, "y": iy + ih, "w": breite, "h": _ZEILE}),
    ]
    for name, spalte, zeile in (("Quick Wins", 0, 0), ("Große Vorhaben", 1, 0), ("Nebenbei", 0, 1),
                                ("Lieber nicht", 1, 1)):
        texte.append((name, {"x": ix + spalte * qw + _ABSTAND, "y": iy + zeile * qh + _ABSTAND,
                             "w": qw - 2 * _ABSTAND, "h": _ZEILE}))
    striche = [
        _linie(ix, iy, ix, iy + ih),  # y-Achse (Nutzen), von oben nach unten
        _linie(ix, iy + ih, ix + iw, iy + ih),  # x-Achse (Aufwand), von links nach rechts
        _linie(ix + qw, iy, ix + qw, iy + ih),  # senkrechte Teilung
        _linie(ix, iy + qh, ix + iw, iy + qh),  # waagrechte Teilung
    ]
    return Vorlagenplan(texte, striche)


def _onepager(feld: dict[str, int]) -> Vorlagenplan:
    hoehe = feld["h"] // len(_ABSCHNITTE)
    texte: list[tuple[str, dict[str, int]]] = []
    striche: list[list[dict[str, int]]] = []
    for i, titel in enumerate(_ABSCHNITTE):
        oben = feld["y"] + i * hoehe
        texte.append((titel, {"x": feld["x"] + _ABSTAND, "y": oben + _ABSTAND, "w": feld["w"] - 2 * _ABSTAND,
                              "h": _ZEILE}))
        if i < len(_ABSCHNITTE) - 1:
            striche.append(_linie(feld["x"], oben + hoehe, feld["x"] + feld["w"], oben + hoehe))
    return Vorlagenplan(texte, striche)


def vorlagenplan(art: str, feld: dict[str, int]) -> Vorlagenplan | None:
    """Plan der Vorlage `art` im Feld `feld`; eine unbekannte Art ergibt None."""
    if art == "matrix":
        return _matrix(feld)
    if art == "onepager":
        return _onepager(feld)
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
