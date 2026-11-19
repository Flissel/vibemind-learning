"""A diagram on a task: a list of boxes and arrows, and the Graphviz text built from it.

A model only ever supplies the list. Identifiers, attributes and layout are written here, so no text from a
model reaches Graphviz as anything but a quoted label (Graphviz text can otherwise pull local files into
the picture). Drawing happens elsewhere (`claw/zeichner.py`); this module is pure.
"""

from __future__ import annotations

import textwrap
from typing import Literal

from pydantic import Field, model_validator

from spaces.learning.services.course_factory.schemas import _StrictDraft

MAX_KNOTEN = 30
MAX_KANTEN = 60
_ID = r"^[a-z][a-z0-9_]{0,23}$"
_UMBRUCH = 24
_RICHTUNG = {"oben-unten": "TB", "links-rechts": "LR"}


class Knoten(_StrictDraft):
    """One box. `leer`: shown with a number instead of its text, for learners to name it."""

    id: str = Field(pattern=_ID)
    text: str = Field(min_length=1, max_length=80)
    leer: bool = False


class Kante(_StrictDraft):
    von: str = Field(pattern=_ID)
    nach: str = Field(pattern=_ID)
    text: str | None = Field(None, min_length=1, max_length=40)


class Gruppe(_StrictDraft):
    """Boxes drawn inside one frame with a title."""

    titel: str = Field(min_length=1, max_length=60)
    knoten: list[str] = Field(min_length=1, max_length=MAX_KNOTEN)


class Diagramm(_StrictDraft):
    richtung: Literal["oben-unten", "links-rechts"] = "oben-unten"
    knoten: list[Knoten] = Field(min_length=1, max_length=MAX_KNOTEN)
    kanten: list[Kante] = Field(default_factory=list, max_length=MAX_KANTEN)
    gruppen: list[Gruppe] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _bezuege(self) -> Diagramm:
        ids = [k.id for k in self.knoten]
        if len(set(ids)) != len(ids):
            raise ValueError("Jede Kennung eines Kastens darf nur einmal vorkommen")
        bekannt = set(ids)
        for kante in self.kanten:
            for ende in (kante.von, kante.nach):
                if ende not in bekannt:
                    raise ValueError(f"Ein Pfeil nennt den Kasten „{ende}“, den es nicht gibt")
        vergeben: set[str] = set()
        for gruppe in self.gruppen:
            for mitglied in gruppe.knoten:
                if mitglied not in bekannt:
                    raise ValueError(f"Eine Gruppe nennt den Kasten „{mitglied}“, den es nicht gibt")
                if mitglied in vergeben:
                    raise ValueError(f"Der Kasten „{mitglied}“ steht in zwei Gruppen")
                vergeben.add(mitglied)
        return self


def _zitat(text: str, *, umbruch: bool = True) -> str:
    """`text` as a quoted Graphviz label: backslashes and quotes escaped, so neither an attribute nor an
    escape sequence (`\\N`, `\\G`, …) can come out of it. The only sequence is our own line break."""
    glatt = " ".join(text.split())
    zeilen = textwrap.wrap(glatt, width=_UMBRUCH) if umbruch else [glatt]
    sicher = [z.replace("\\", "\\\\").replace('"', '\\"') for z in zeilen or [""]]
    return '"' + "\\n".join(sicher) + '"'


def dot_text(diagramm: Diagramm) -> str:
    """The Graphviz text of `diagramm`. Boxes are called n0, n1, … in list order; an empty box carries its
    number (1, 2, … among the empty ones) and a dashed frame, never its text."""
    name = {k.id: f"n{i}" for i, k in enumerate(diagramm.knoten)}
    zeilen = [
        "digraph G {",
        f'  graph [rankdir={_RICHTUNG[diagramm.richtung]}, bgcolor="white", fontname="Arial", fontsize=13, '
        'nodesep=0.35, ranksep=0.45, pad=0.2, size="10.4,14"];',
        '  node [shape=box, style="rounded", fontname="Arial", fontsize=14, margin="0.18,0.10", penwidth=1.3];',
        '  edge [fontname="Arial", fontsize=12, arrowsize=0.8];',
    ]
    nummer = 0
    for knoten in diagramm.knoten:
        if knoten.leer:
            nummer += 1
            zeilen.append(f'  {name[knoten.id]} [label="{nummer}", style="rounded,dashed", width=1.9, height=0.7];')
        else:
            zeilen.append(f"  {name[knoten.id]} [label={_zitat(knoten.text)}];")
    for i, gruppe in enumerate(diagramm.gruppen):
        mitglieder = " ".join(f"{name[m]};" for m in gruppe.knoten)
        zeilen.append(f"  subgraph cluster_{i} {{ label={_zitat(gruppe.titel, umbruch=False)}; "
                      f'style="rounded,dashed"; color="gray40"; {mitglieder} }}')
    for kante in diagramm.kanten:
        pfeil = f"  {name[kante.von]} -> {name[kante.nach]}"
        zeilen.append(f"{pfeil} [label={_zitat(kante.text)}];" if kante.text else f"{pfeil};")
    zeilen.append("}")
    return "\n".join(zeilen) + "\n"
