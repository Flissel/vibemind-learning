"""Werkstatt model roles: rough plan, one task with its lesson section, and the checker.

At most three model calls per task: author, one repair on findings, checker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from spaces.learning.services.course_factory.chapter_verification import keyed_source_refs
from spaces.learning.services.course_factory.model_gateway import (
    ROLE_CONTRACTS,
    ModelGateway,
    ModelInvocation,
)
from spaces.learning.services.course_factory.verification import SourceSnapshot
from spaces.learning.services.werkstatt.inhalt import AufgabeInhalt, Fach, Grobentwurf
from spaces.learning.services.werkstatt.pruefung import (
    Befund,
    aufgabe_pruefen,
    mit_quellenschluesseln,
    quellen_aufloesen,
)


class AufgabeAusgabe(BaseModel):
    """Wrapper: the gateway needs a BaseModel, the task is a discriminated union."""

    schema_version: Literal["werkstatt-aufgabe-v1"]
    aufgabe: AufgabeInhalt


class PrueferIssue(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2_000)


class PrueferAusgabe(BaseModel):
    schema_version: Literal["werkstatt-pruefer-v1"]
    issues: list[PrueferIssue] = Field(default_factory=list, max_length=50)


@dataclass(frozen=True)
class AusformulierErgebnis:
    inhalt: dict[str, Any]
    zitate: list[dict[str, Any]]
    befunde: list[Befund]


def _invocation(role: str, correlation_id: str, payload: dict[str, object]) -> ModelInvocation:
    stage, prompt_version, schema_version = ROLE_CONTRACTS[role]
    return ModelInvocation(
        role=role,
        stage=stage,
        correlation_id=correlation_id,
        prompt_version=prompt_version,
        output_schema_version=schema_version,
        input_payload=payload,
    )


def _fuer_modell(keyed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the model sees of each chunk: its key, heading and text. Ids, hashes and
    coordinates stay on the server (keys are resolved there); they were most of the payload."""
    out = []
    for ref in keyed:
        eintrag: dict[str, Any] = {"key": ref["key"]}
        locator = ref.get("locator")
        if isinstance(locator, dict) and locator.get("heading"):
            eintrag["heading"] = locator["heading"]
        if ref.get("content"):
            eintrag["content"] = ref["content"]
        out.append(eintrag)
    return out


async def grobentwurf_erzeugen(
    gateway: ModelGateway,
    *,
    correlation_id: str,
    title: str,
    audience: str,
    outcomes: list[str],
    default_kind: str | None,
    hinweise: list[str],
    source_refs: tuple[str, ...],
) -> Grobentwurf:
    payload: dict[str, object] = {
        "title": title,
        "audience": audience,
        "outcomes": list(outcomes),
        "default_kind": default_kind,
        "source_refs": _fuer_modell(keyed_source_refs(source_refs)),
    }
    if hinweise:
        payload["reviewer_feedback"] = list(hinweise)
    result = await gateway.generate(_invocation("werkstatt_grobentwurf", correlation_id, payload), Grobentwurf)
    return result.output


async def _autor(
    gateway: ModelGateway, correlation_id: str, payload: dict[str, object]
) -> AufgabeInhalt:
    result = await gateway.generate(_invocation("werkstatt_aufgabe", correlation_id, payload), AufgabeAusgabe)
    # The model names sources only by S-key: citation objects it writes itself are ignored.
    return result.output.aufgabe.model_copy(update={"zitate": []})


async def aufgabe_ausformulieren(
    gateway: ModelGateway,
    *,
    correlation_id: str,
    kurs: dict[str, Any],
    summary: str,
    kind: str,
    wunsch: str | None,
    vorherige: dict[str, Any] | None,
    source_refs: tuple[str, ...],
    sources: tuple[SourceSnapshot, ...],
) -> AusformulierErgebnis:
    keyed = keyed_source_refs(source_refs)
    payload: dict[str, object] = {
        "kurs": kurs,
        "summary": summary,
        "kind": kind,
        "source_refs": _fuer_modell(keyed),
    }
    if wunsch:
        payload["wunsch"] = wunsch
    if vorherige is not None:
        # Stored citation ids mean nothing to the model: it sees the S-keys of today's catalog.
        payload["vorherige"] = mit_quellenschluesseln(vorherige, source_refs)
    course_id = str(kurs.get("course_id", ""))
    fach: Fach = "mathe" if kurs.get("fach") == "mathe" else "terminal"

    inhalt, zitate = quellen_aufloesen(
        await _autor(gateway, correlation_id, payload), source_refs, course_id=course_id
    )
    befunde = aufgabe_pruefen(inhalt, sources=sources, fach=fach)
    if befunde:
        reparatur = {**payload, "reviewer_feedback": [b["text"] for b in befunde]}
        inhalt, zitate = quellen_aufloesen(
            await _autor(gateway, correlation_id, reparatur), source_refs, course_id=course_id
        )
        befunde = aufgabe_pruefen(inhalt, sources=sources, fach=fach)
    if inhalt.kind != kind:
        befunde.append(
            {
                "code": "kind_abweichend",
                "text": "Das Modell hat einen anderen Aufgabentyp geliefert als verlangt.",
            }
        )

    zitierte = {z.chunk_id for z in zitate}
    pruefer_payload: dict[str, object] = {
        "aufgabe": mit_quellenschluesseln(inhalt.model_dump(mode="json"), source_refs),
        "source_refs": _fuer_modell([ref for ref in keyed if ref["chunk_id"] in zitierte]),
    }
    pruefung = await gateway.generate(
        _invocation("werkstatt_pruefer", correlation_id, pruefer_payload), PrueferAusgabe
    )
    for issue in pruefung.output.issues:
        code = issue.code if issue.code.startswith("pruefer_") else f"pruefer_{issue.code}"
        befunde.append({"code": code, "text": issue.text})

    return AusformulierErgebnis(
        inhalt=inhalt.model_dump(mode="json"),
        zitate=[z.model_dump(mode="json") for z in zitate],
        befunde=befunde,
    )
