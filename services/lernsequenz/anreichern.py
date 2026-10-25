"""Let the model design drawn explanations, calculations and estimates for a presentation.

The model only proposes; every proposal is validated against the sequence contract and
grounded deterministically: each number in a calculation or estimate must appear in the
slide text it belongs to (plus a few unit constants), drawings must stay on the board,
and refs are taken from the slide, never from the model. Rejected proposals are reported.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from spaces.learning.contracts.lernsequenz import (
    Calculation,
    Checkpoint,
    DrawnExplanation,
    Estimate,
    LernsequenzV1,
    Slide,
)
from spaces.learning.services.course_factory.model_gateway import ModelGateway, ModelInvocation

# Only conversion constants (percent, minutes, hours, months, thousands) need not be on the slide.
_UNIT_CONSTANTS = {0.0, 1.0, 12.0, 24.0, 60.0, 100.0, 1000.0}
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_CHECKPOINT = TypeAdapter(Checkpoint)
_MAX_NEW = 6
_DEFAULT_TITLES = {"drawn_explanation": "An der Tafel erklärt", "calculation": "Nachrechnen", "estimate": "Schätzen"}


class SequenceDesignOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["sequence-design-v1"]
    checkpoints: list[dict[str, Any]] = Field(max_length=12)


def _numbers(text: str) -> set[float]:
    found = set()
    for raw in _NUMBER.findall(text):
        cleaned = raw.replace(".", "").replace(",", ".") if re.fullmatch(r"\d{1,3}(\.\d{3})+", raw) else raw.replace(",", ".")
        try:
            found.add(float(cleaned))
        except ValueError:
            continue
    return found


def _normalized(proposal: dict[str, Any]) -> dict[str, Any]:
    """Models write SVG strokes as {"d": ...}; the contract calls it "path"."""
    if proposal.get("kind") != "drawn_explanation" or not isinstance(proposal.get("steps"), list):
        return proposal
    steps = []
    for step in proposal["steps"]:
        if isinstance(step, dict) and isinstance(step.get("strokes"), list):
            strokes = [
                {"path": s.get("path", s.get("d")), **({"color": s["color"]} if "color" in s else {})}
                if isinstance(s, dict) else {"path": s}
                for s in step["strokes"]
            ]
            step = {**step, "strokes": strokes}
        steps.append(step)
    return {**proposal, "steps": steps}


def _slide_text(slide: Slide) -> str:
    return " ".join(filter(None, [slide.title, slide.statement, *slide.points, slide.narration]))


def _grounded(checkpoint: Checkpoint, slide_text: str) -> str | None:
    """None if fine, else the reason the proposal is rejected."""
    allowed = _numbers(slide_text) | _UNIT_CONSTANTS
    if isinstance(checkpoint, Calculation):
        used = _numbers(checkpoint.task) | {n for step in checkpoint.steps for n in _numbers(re.sub(r"s[1-9]", "", step.expected))}
    elif isinstance(checkpoint, Estimate):
        used = {checkpoint.value, checkpoint.good_from, checkpoint.good_to} | _numbers(checkpoint.question)
    elif isinstance(checkpoint, DrawnExplanation):
        for step in checkpoint.steps:
            for stroke in step.strokes:
                if any(n > 800 for n in _numbers(stroke.path)):
                    return "drawing leaves the board"
            for label in step.labels:
                if label.x > checkpoint.width or label.y > checkpoint.height:
                    return "label leaves the board"
        return None
    else:
        return "unsupported checkpoint kind"
    invented = sorted(n for n in used if n not in allowed)
    return f"numbers not on the slide: {invented[:5]}" if invented else None


async def enrich(
    gateway: ModelGateway, sequence: LernsequenzV1, *, correlation_id: str
) -> tuple[LernsequenzV1, list[str]]:
    slides = {s.section_id: s for s in sequence.sections if isinstance(s, Slide)}
    if not slides:
        raise ValueError("only slides can be enriched")
    result = await gateway.generate(
        ModelInvocation(
            role="sequence_designer",
            stage="sequencing",
            correlation_id=correlation_id,
            prompt_version="sequence-design-prompt-v1",
            output_schema_version="sequence-design-v1",
            input_payload={
                "course_title": sequence.title,
                "slides": [{"section_id": sid, "text": _slide_text(s)} for sid, s in slides.items()],
                "kinds": ["drawn_explanation", "calculation", "estimate"],
            },
        ),
        SequenceDesignOutput,
    )
    report: list[str] = []
    added: list[Checkpoint] = []
    existing = {c.checkpoint_id for c in sequence.checkpoints}
    for index, proposal in enumerate(result.output.checkpoints, start=1):
        slide = slides.get(str(proposal.get("after_section")))
        if slide is None:
            report.append(f"#{index}: unknown slide")
            continue
        candidate = {
            "title": _DEFAULT_TITLES.get(str(proposal.get("kind")), "Haltepunkt"),
            **_normalized(proposal),
            "checkpoint_id": f"ki-{index}-{proposal.get('kind', 'x')}",
            "refs": list(slide.refs),
            "at_s": None,
        }
        try:
            checkpoint = _CHECKPOINT.validate_python(candidate)
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            report.append(f"#{index}: invalid ({error.error_count()} errors; {where}: {first['msg'][:120]})")
            continue
        reason = _grounded(checkpoint, _slide_text(slide))
        if reason or checkpoint.checkpoint_id in existing:
            report.append(f"#{index}: rejected, {reason or 'duplicate id'}")
            continue
        added.append(checkpoint)
        report.append(f"#{index}: added {checkpoint.kind} after {slide.section_id}")
        if len(added) >= _MAX_NEW:
            break
    enriched = sequence.model_copy(update={"checkpoints": (*sequence.checkpoints, *added)})
    return LernsequenzV1.model_validate(enriched.model_dump()), report
