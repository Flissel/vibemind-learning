"""Werkstatt task content schemas and the rule that classifies a new version."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from spaces.learning.services.course_factory.schemas import (
    Citation,
    Claim,
    QuizOptionDraft,
    RubricCriterion,
    _StrictDraft,
)

_PLATZHALTER = re.compile(r"\{\{([^{}]+)\}\}")


class Lektionsabschnitt(_StrictDraft):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=100_000)
    claims: list[Claim] = Field(default_factory=list, max_length=2_000)
    concept_ids: list[str] = Field(default_factory=list, max_length=200)


class _AufgabeBasis(_StrictDraft):
    prompt: str = Field(min_length=1, max_length=20_000)
    concept_ids: list[str] = Field(min_length=1, max_length=200)
    citation_ids: list[str] = Field(default_factory=list, max_length=100)
    zitate: list[Citation] = Field(default_factory=list, max_length=100)
    lektion: Lektionsabschnitt | None = None


class Quiz(_AufgabeBasis):
    kind: Literal["quiz"]
    options: list[QuizOptionDraft] = Field(min_length=2, max_length=6)
    correct_option_ids: list[str] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def _richtige_gehoeren_zu_optionen(self) -> Quiz:
        ids = {o.option_id for o in self.options}
        if len(ids) != len(self.options):
            raise ValueError("option_id muss eindeutig sein")
        if not set(self.correct_option_ids) <= ids:
            raise ValueError("correct_option_ids verweist auf unbekannte Option")
        return self


class Offen(_AufgabeBasis):
    kind: Literal["open"]
    expected_answer: str = Field(max_length=50_000)
    rubric: list[RubricCriterion] = Field(min_length=1, max_length=100)


class Fall(_AufgabeBasis):
    kind: Literal["case"]
    expected_answer: str = Field(max_length=50_000)
    rubric: list[RubricCriterion] = Field(min_length=1, max_length=100)


class LueckeFeld(_StrictDraft):
    blank_id: str = Field(min_length=1, max_length=50)
    accepted: list[str] = Field(min_length=1, max_length=50)


class Luecke(_AufgabeBasis):
    kind: Literal["cloze"]
    template: str = Field(min_length=1, max_length=20_000)
    blanks: list[LueckeFeld] = Field(min_length=1, max_length=50)
    shell: Literal["powershell", "cmd", "unix"] | None = None

    @model_validator(mode="after")
    def _platzhalter_passen_zu_luecken(self) -> Luecke:
        ids = [b.blank_id for b in self.blanks]
        if len(set(ids)) != len(ids):
            raise ValueError("blank_id muss eindeutig sein")
        if set(_PLATZHALTER.findall(self.template)) != set(ids):
            raise ValueError("Platzhalter im template passen nicht zu blanks")
        return self


class Reihenfolge(_AufgabeBasis):
    kind: Literal["ordering"]
    items: list[str] = Field(min_length=3, max_length=8)


class Paar(_StrictDraft):
    left: str = Field(min_length=1, max_length=2_000)
    right: str = Field(min_length=1, max_length=2_000)


class Zuordnung(_AufgabeBasis):
    kind: Literal["matching"]
    pairs: list[Paar] = Field(min_length=3, max_length=6)


AufgabeInhalt = Annotated[
    Quiz | Offen | Fall | Luecke | Reihenfolge | Zuordnung,
    Field(discriminator="kind"),
]

_ADAPTER: TypeAdapter[AufgabeInhalt] = TypeAdapter(AufgabeInhalt)


class GrobAufgabe(_StrictDraft):
    summary: str = Field(min_length=1, max_length=2_000)


class GrobKapitel(_StrictDraft):
    title: str = Field(min_length=1, max_length=300)
    tasks: list[GrobAufgabe] = Field(min_length=1, max_length=200)


class Grobentwurf(_StrictDraft):
    schema_version: Literal["werkstatt-grobentwurf-v1"]
    title: str = Field(min_length=1, max_length=500)
    audience: str = Field(min_length=1, max_length=1_000)
    default_kind: Literal["quiz", "open", "case", "cloze", "ordering", "matching"]
    chapters: list[GrobKapitel] = Field(min_length=1, max_length=500)


def inhalt_lesen(content: dict[str, Any]) -> AufgabeInhalt:
    return _ADAPTER.validate_python(content)


def _loesung(inhalt: BaseModel) -> object:
    """Everything that makes an answer right or wrong, order-insensitive where order is free."""
    if isinstance(inhalt, Quiz):
        return (
            sorted(inhalt.correct_option_ids),
            sorted((o.option_id, o.text) for o in inhalt.options),
        )
    if isinstance(inhalt, (Offen, Fall)):
        rubrik = sorted(
            (r.criterion_id, r.description, r.points) for r in inhalt.rubric
        )
        return (inhalt.expected_answer, rubrik)
    if isinstance(inhalt, Luecke):
        return (
            inhalt.template,
            inhalt.shell,
            sorted((b.blank_id, sorted(b.accepted)) for b in inhalt.blanks),
        )
    if isinstance(inhalt, Reihenfolge):
        return list(inhalt.items)
    if isinstance(inhalt, Zuordnung):
        return sorted((p.left, p.right) for p in inhalt.pairs)
    raise TypeError(type(inhalt).__name__)


def fassungs_art(alt: dict[str, Any] | None, neu: dict[str, Any]) -> Literal["neu", "textkorrektur"]:
    """'neu' if solution, options, kind or concepts changed (or no predecessor)."""
    if alt is None:
        return "neu"
    a, n = inhalt_lesen(alt), inhalt_lesen(neu)
    if a.kind != n.kind or sorted(a.concept_ids) != sorted(n.concept_ids):
        return "neu"
    return "neu" if _loesung(a) != _loesung(n) else "textkorrektur"
