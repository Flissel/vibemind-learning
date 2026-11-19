"""A picture on a task: either a copy from Wikimedia Commons or a diagram drawn from a list of boxes.

The picture itself is a stored copy in the media folder (`/lernsequenz-medien/bild-<hash>.<ext>`); this
module only holds what is recorded next to it: for Commons the licence rule and the source line (title,
author and licence come from Commons, never from a model), for a diagram the list it was drawn from.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from spaces.learning.services.course_factory.schemas import _StrictDraft
from spaces.learning.services.werkstatt.diagramm import Diagramm

# Licences that allow commercial use and changes. The pattern is closed on purpose: non-commercial (NC),
# no-derivatives (ND), GFDL and anything unknown fail, and so does a licence name with trailing conditions.
_FREI = re.compile(r"^(?:cc0(?: 1\.0)?|public domain|cc by(?:-sa)? [1-4]\.[0-9](?: [a-z]{2,3})?)$", re.IGNORECASE)
_BILDPFAD = r"^/lernsequenz-medien/bild-[0-9a-f]{16}\.(?:png|jpg)$"
_COMMONS = r"^https://commons\.wikimedia\.org/wiki/File:[^\s]{1,400}$"
_HTTPS = r"^https://[^\s]{1,400}$"
HERKUNFT = "Wikimedia Commons"
EIGEN = "eigene Darstellung"


def lizenz_erlaubt(kurzname: str) -> bool:
    """Whether a Commons licence short name ("CC BY-SA 4.0") may be used in a course."""
    return bool(_FREI.fullmatch(kurzname.strip()))


class Bild(_StrictDraft):
    """One picture of an open task or a drawing task. `herkunft` "commons": with what its licence asks us
    to name. `herkunft` "diagramm": our own drawing, with the list it was drawn from and no licence."""

    image: str = Field(pattern=_BILDPFAD)
    width: int = Field(ge=1, le=10_000)
    height: int = Field(ge=1, le=10_000)
    titel: str = Field(min_length=1, max_length=300)
    herkunft: Literal["commons", "diagramm"] = "commons"
    urheber: str | None = Field(None, min_length=1, max_length=300)
    lizenz: str | None = Field(None, min_length=1, max_length=60)
    lizenz_url: str | None = Field(None, pattern=_HTTPS)
    quelle_url: str | None = Field(None, pattern=_COMMONS)
    diagramm: Diagramm | None = None

    @field_validator("lizenz")
    @classmethod
    def _nur_freie_lizenz(cls, wert: str | None) -> str | None:
        if wert is None:
            return None
        if not lizenz_erlaubt(wert):
            raise ValueError("Lizenz erlaubt keine gewerbliche Nutzung mit Änderungen oder ist unbekannt")
        return wert.strip()

    @model_validator(mode="after")
    def _angaben_zur_herkunft(self) -> Bild:
        fremd = (self.urheber, self.lizenz, self.quelle_url)
        if self.herkunft == "commons":
            if any(wert is None for wert in fremd):
                raise ValueError("Ein Bild von Wikimedia Commons braucht Urheber, Lizenz und Dateiseite")
            if self.diagramm is not None:
                raise ValueError("Ein Bild von Wikimedia Commons trägt keine Diagramm-Liste")
        else:
            if self.diagramm is None:
                raise ValueError("Ein Diagramm braucht die Liste, aus der es gezeichnet wurde")
            if any(wert is not None for wert in (*fremd, self.lizenz_url)):
                raise ValueError("Eine eigene Darstellung nennt weder Urheber noch Lizenz noch Dateiseite")
        return self


def quellenzeile(bild: Bild) -> str:
    """"Bild: <Titel>, <Urheber>, <Lizenz>, Wikimedia Commons" or "Diagramm: <Titel>, eigene Darstellung":
    the line shown wherever the picture is."""
    if bild.herkunft == "diagramm":
        return f"Diagramm: {bild.titel}, {EIGEN}"
    return f"Bild: {bild.titel}, {bild.urheber}, {bild.lizenz}, {HERKUNFT}"


def darstellung(bild: Bild) -> dict[str, Any]:
    """What learners get: the picture (as a top-level `image`, which the Hülle puts next to the task) and
    its source. The list of a diagram stays behind: it holds the texts of the boxes left empty."""
    quelle: dict[str, Any]
    if bild.herkunft == "diagramm":
        quelle = {"titel": bild.titel, "eigen": True}
    else:
        quelle = {"titel": bild.titel, "urheber": bild.urheber, "lizenz": bild.lizenz,
                  "lizenz_url": bild.lizenz_url, "quelle_url": bild.quelle_url}
    return {"image": bild.image, "width": bild.width, "height": bild.height, "bildquelle": quelle}
