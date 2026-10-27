"""Tutor inside the sequence player: knows the slide or checkpoint the learner is at.

The context is built on the server from the sequence, never with solutions (no correct
options, no expected formulas, no estimate value). The model may only cite the given
source keys; anything else is rejected like in the practice tutor.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from spaces.learning.contracts.lernsequenz import (
    Calculation,
    Choice,
    CommandCheck,
    DrawnExplanation,
    Estimate,
    Explanation,
    LernsequenzV1,
    Slide,
    VideoSegment,
)
from spaces.learning.services.course_factory.model_gateway import ModelGateway, ModelImageInput, ModelInvocation
from spaces.learning.mcp.tools.tutor import TutorModelOutput

_MAX_SOURCES = 12
_MAX_SELECTION = 2_000


class SequenceExplainOutput(BaseModel):
    """What the model returns for a marked place: the explanation from the course, and an
    optional addition from outside it, always kept apart."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["sequence-explain-v1"]
    explanation: str = Field(min_length=1, max_length=10_000)
    source_refs: list[str] = Field(min_length=1, max_length=100)
    extra: str = Field(default="", max_length=10_000)
    extra_is_outside_course: bool = True
    extra_sources: list[str] = Field(default_factory=list, max_length=20)


def tutor_context(
    sequence: LernsequenzV1, *, section_id: str | None = None, checkpoint_id: str | None = None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """(task, sources) for the tutor prompt; sources are short keyed texts from the sequence."""
    texts: list[str] = []
    if checkpoint_id:
        checkpoint = sequence.checkpoint(checkpoint_id)
        if isinstance(checkpoint, Choice):
            task = {"prompt": checkpoint.question, "type": "choice_checkpoint",
                    "options": [o.text for o in checkpoint.options]}
        elif isinstance(checkpoint, Calculation):
            task = {"prompt": checkpoint.task, "type": "calculation",
                    "steps": [{"title": s.title, "unit": s.unit, "formula": s.formula_shown} for s in checkpoint.steps]}
        elif isinstance(checkpoint, Estimate):
            task = {"prompt": checkpoint.question, "type": "estimate", "unit": checkpoint.unit}
        elif isinstance(checkpoint, DrawnExplanation):
            task = {"prompt": checkpoint.draw_task or checkpoint.title, "type": "drawn_explanation"}
            texts += [step.narration for step in checkpoint.steps]
        elif isinstance(checkpoint, CommandCheck):
            task = {"prompt": checkpoint.task, "type": "command_check",
                    "commands": [f"{c.shell}: {c.command}" for c in checkpoint.commands]}
        else:
            assert isinstance(checkpoint, Explanation)
            task = {"prompt": checkpoint.title, "type": "explanation"}
            texts.append(checkpoint.text)
        anchor = checkpoint.after_section
    else:
        anchor = section_id or sequence.sections[0].section_id
        task = None
    section = next((s for s in sequence.sections if s.section_id == anchor), None)
    if section is None:
        raise LookupError("section is unknown")
    if isinstance(section, Slide):
        texts.insert(0, " ".join(filter(None, [section.title, section.statement, *section.points, section.narration])))
    elif isinstance(section, VideoSegment) and section.transcript:
        texts.insert(0, section.transcript)
    if task is None:
        task = {"prompt": section.title, "type": "explanation"}
    sources = [{"key": f"S{i}", "source": text} for i, text in enumerate(texts[:_MAX_SOURCES], start=1) if text]
    if not sources:
        raise LookupError("no tutor sources at this point")
    return task, sources


async def ask_tutor(
    gateway: ModelGateway, *, question: str, task: dict[str, Any], sources: list[dict[str, str]], correlation_id: str
) -> dict[str, Any]:
    if not question.strip() or len(question) > 2_000:
        raise ValueError("tutor question is invalid")
    result = await gateway.generate(
        ModelInvocation(
            role="learning_tutor",
            stage="tutoring",
            correlation_id=correlation_id,
            prompt_version="learning-tutor-prompt-v2",
            output_schema_version="learning-tutor-v1",
            input_payload={
                "question": question.strip(),
                "task": task,
                "sources": sources,
                "policy": "guide_without_revealing_expected_answer",
            },
        ),
        TutorModelOutput,
    )
    keys = {source["key"] for source in sources}
    if not result.output.source_refs or not set(result.output.source_refs) <= keys:
        raise ValueError("tutor returned unsupported source references")
    return {
        "answer": result.output.answer,
        "next_step": result.output.next_step,
        "source_keys": list(result.output.source_refs),
        "evidence_ref": result.evidence_ref,
    }


def explain_context(
    sequence: LernsequenzV1, *, anchor: dict[str, Any], selection: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """(task, sources) for explaining a marked place: the marked text and the slide or
    checkpoint it sits on, built like the tutor's context (never a solution)."""
    text = " ".join(selection.split()) if isinstance(selection, str) else ""
    if not text or len(text) > _MAX_SELECTION:
        raise ValueError("selection is required (up to 2000 characters)")
    section_id, checkpoint_id = anchor.get("section_id"), anchor.get("checkpoint_id")
    at, sources = tutor_context(
        sequence,
        section_id=section_id if isinstance(section_id, str) else None,
        checkpoint_id=checkpoint_id if isinstance(checkpoint_id, str) else None,
    )
    return {"type": "explain", "prompt": "Erkläre die markierte Stelle.", "selection": text, "at": at}, sources


async def ask_explain(
    gateway: ModelGateway, *, task: dict[str, Any], sources: list[dict[str, str]], correlation_id: str,
    image_png: bytes | None = None,
) -> dict[str, Any]:
    images: tuple[ModelImageInput, ...] = ()
    if image_png is not None:
        images = (ModelImageInput(media_type="image/png", content=image_png, sha256=hashlib.sha256(image_png).hexdigest()),)
    result = await gateway.generate(
        ModelInvocation(
            role="sequence_explainer",
            stage="tutoring",
            correlation_id=correlation_id,
            prompt_version="sequence-explain-prompt-v1",
            output_schema_version="sequence-explain-v1",
            input_payload={"task": task, "sources": sources, "policy": "explain_from_sources_mark_anything_else_as_extra"},
            image_inputs=images,
        ),
        SequenceExplainOutput,
    )
    keys = {source["key"] for source in sources}
    if not result.output.source_refs or not set(result.output.source_refs) <= keys:
        raise ValueError("explanation returned unsupported source references")
    extra = result.output.extra.strip()
    return {
        "explanation": result.output.explanation,
        "source_keys": list(result.output.source_refs),
        "extra": extra,
        # Whatever the model says: an addition is never passed off as course content.
        "extra_is_outside_course": bool(extra),
        "extra_sources": list(result.output.extra_sources) if extra else [],
        "evidence_ref": result.evidence_ref,
    }
