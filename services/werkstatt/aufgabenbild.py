"""A picture on a task, taken from Wikimedia Commons: the licence rule and the source line.

The picture itself is a stored copy in the media folder (`/lernsequenz-medien/bild-<hash>.<ext>`); this
module only holds what is recorded next to it. Whoever stores a picture reads title, author and licence
from Commons, never from a model.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field, field_validator

from spaces.learning.services.course_factory.schemas import _StrictDraft

# Licences that allow commercial use and changes. The pattern is closed on purpose: non-commercial (NC),
# no-derivatives (ND), GFDL and anything unknown fail, and so does a licence name with trailing conditions.
_FREI = re.compile(r"^(?:cc0(?: 1\.0)?|public domain|cc by(?:-sa)? [1-4]\.[0-9](?: [a-z]{2,3})?)$", re.IGNORECASE)
_BILDPFAD = r"^/lernsequenz-medien/bild-[0-9a-f]{16}\.(?:png|jpg)$"
_COMMONS = r"^https://commons\.wikimedia\.org/wiki/File:[^\s]{1,400}$"
_HTTPS = r"^https://[^\s]{1,400}$"
HERKUNFT = "Wikimedia Commons"


def lizenz_erlaubt(kurzname: str) -> bool:
    """Whether a Commons licence short name ("CC BY-SA 4.0") may be used in a course."""
    return bool(_FREI.fullmatch(kurzname.strip()))


class Bild(_StrictDraft):
    """One picture of an open task or a drawing task, with what its licence asks us to name."""

    image: str = Field(pattern=_BILDPFAD)
    width: int = Field(ge=1, le=10_000)
    height: int = Field(ge=1, le=10_000)
    titel: str = Field(min_length=1, max_length=300)
    urheber: str = Field(min_length=1, max_length=300)
    lizenz: str = Field(min_length=1, max_length=60)
    lizenz_url: str | None = Field(None, pattern=_HTTPS)
    quelle_url: str = Field(pattern=_COMMONS)

    @field_validator("lizenz")
    @classmethod
    def _nur_freie_lizenz(cls, wert: str) -> str:
        if not lizenz_erlaubt(wert):
            raise ValueError("Lizenz erlaubt keine gewerbliche Nutzung mit Änderungen oder ist unbekannt")
        return wert.strip()


def quellenzeile(bild: Bild) -> str:
    """"Bild: <Titel>, <Urheber>, <Lizenz>, Wikimedia Commons": the line shown wherever the picture is."""
    return f"Bild: {bild.titel}, {bild.urheber}, {bild.lizenz}, {HERKUNFT}"


def darstellung(bild: Bild) -> dict[str, Any]:
    """What learners get: the picture (as a top-level `image`, which the Hülle puts next to the task) and
    its source."""
    return {
        "image": bild.image, "width": bild.width, "height": bild.height,
        "bildquelle": {"titel": bild.titel, "urheber": bild.urheber, "lizenz": bild.lizenz,
                       "lizenz_url": bild.lizenz_url, "quelle_url": bild.quelle_url},
    }
