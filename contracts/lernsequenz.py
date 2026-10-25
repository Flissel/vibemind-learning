"""Learning sequences: a presentation or video with checkpoints, played by Learning itself.

A sequence is an ordered list of sections (slides or video segments) and checkpoints that
pause it: an explanation, a choice question, a calculation with steps, an estimate or an
explanation drawn stroke by stroke. LearnHouse pages embed the player; the player only
ever receives ``public_view`` (no solutions), answers are graded on the server.
"""

from __future__ import annotations

import ast
import re
from typing import Annotated, Literal, Union

from pydantic import Field, field_validator, model_validator

from spaces.learning.contracts.mcp_models import ContractModel, SafeToken

Text = Annotated[str, Field(min_length=1, max_length=2_000)]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]
Refs = Annotated[tuple[Annotated[str, Field(min_length=1, max_length=200)], ...], Field(max_length=20)]
Color = Literal["ink", "accent", "warn", "blue"]
_PATH = re.compile(r"^[MmLlHhVvCcQqSsTtZz0-9 ,.\-]{1,4000}$")
_FORMULA = re.compile(r"^[0-9s+\-*/(). ]{1,200}$")
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
)


def evaluate_formula(formula: str, previous: dict[str, float]) -> float:
    """Evaluate a step formula (numbers, + - * /, parentheses, s1..s9) without ``eval``."""
    tree = ast.parse(formula, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError("formula uses an unsupported construct")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("formula constants must be numbers")
        if isinstance(node, ast.Name) and not re.fullmatch(r"s[1-9]", node.id):
            raise ValueError("formula names must be s1..s9")

    def value(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return value(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in previous:
                raise ValueError(f"formula refers to unknown step {node.id}")
            return previous[node.id]
        if isinstance(node, ast.UnaryOp):
            inner = value(node.operand)
            return -inner if isinstance(node.op, ast.USub) else inner
        assert isinstance(node, ast.BinOp)
        left, right = value(node.left), value(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise ValueError("division by zero")
        return left / right

    return value(tree)


# --- Sections -----------------------------------------------------------------

class Slide(ContractModel):
    kind: Literal["slide"] = "slide"
    section_id: SafeToken
    title: ShortText
    statement: Text | None = None
    points: Annotated[tuple[ShortText, ...], Field(max_length=8)] = ()
    narration: Text
    refs: Refs = ()


class VideoSegment(ContractModel):
    kind: Literal["video"] = "video"
    section_id: SafeToken
    title: ShortText
    url: Annotated[str, Field(min_length=8, max_length=1_000)]
    start_s: Annotated[float, Field(ge=0)] = 0
    end_s: Annotated[float, Field(gt=0)] | None = None
    transcript: Text | None = None
    refs: Refs = ()

    @field_validator("url")
    @classmethod
    def https_or_local(cls, value: str) -> str:
        if not (value.startswith("https://") or value.startswith("/lernsequenz-medien/")):
            raise ValueError("video url must be https or a Learning media path")
        return value


Section = Annotated[Union[Slide, VideoSegment], Field(discriminator="kind")]


# --- Checkpoints --------------------------------------------------------------

class _Checkpoint(ContractModel):
    checkpoint_id: SafeToken
    after_section: SafeToken
    at_s: Annotated[float, Field(ge=0)] | None = None
    title: ShortText
    refs: Refs = ()


class Explanation(_Checkpoint):
    kind: Literal["explanation"] = "explanation"
    text: Text


class Option(ContractModel):
    option_id: SafeToken
    text: ShortText


class Choice(_Checkpoint):
    kind: Literal["choice"] = "choice"
    question: Text
    options: Annotated[tuple[Option, ...], Field(min_length=2, max_length=6)]
    correct: Annotated[tuple[SafeToken, ...], Field(min_length=1, max_length=6)]
    rationale: Text

    @model_validator(mode="after")
    def correct_are_options(self) -> "Choice":
        ids = [option.option_id for option in self.options]
        if len(set(ids)) != len(ids) or not set(self.correct) <= set(ids):
            raise ValueError("choice options must be unique and contain the correct ids")
        return self


class CalcStep(ContractModel):
    step_id: SafeToken
    title: ShortText
    unit: Annotated[str, Field(max_length=20)] = ""
    formula_shown: Annotated[str, Field(max_length=200)] = ""
    hint: ShortText
    expected: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("expected")
    @classmethod
    def safe_formula(cls, value: str) -> str:
        if not _FORMULA.fullmatch(value):
            raise ValueError("expected formula may only use numbers, s1..s9 and + - * / ( )")
        evaluate_formula(value, {f"s{i}": 1.0 for i in range(1, 10)})
        return value


class Calculation(_Checkpoint):
    kind: Literal["calculation"] = "calculation"
    task: Text
    steps: Annotated[tuple[CalcStep, ...], Field(min_length=1, max_length=9)]
    tolerance_percent: Annotated[float, Field(ge=0, le=20)] = 1.0

    @model_validator(mode="after")
    def steps_only_look_back(self) -> "Calculation":
        for index, step in enumerate(self.steps, start=1):
            used = {int(name[1]) for name in re.findall(r"s[1-9]", step.expected)}
            if any(number >= index for number in used):
                raise ValueError("a step may only use results of earlier steps")
        return self


class Estimate(_Checkpoint):
    kind: Literal["estimate"] = "estimate"
    question: Text
    minimum: float
    maximum: float
    step: Annotated[float, Field(gt=0)] = 1
    unit: Annotated[str, Field(max_length=20)] = ""
    good_from: float
    good_to: float
    value: float
    rationale: Text

    @model_validator(mode="after")
    def band_inside_range(self) -> "Estimate":
        if not (self.minimum <= self.good_from <= self.value <= self.good_to <= self.maximum):
            raise ValueError("estimate band must lie inside the range and contain the value")
        return self


class Stroke(ContractModel):
    path: Annotated[str, Field(min_length=2, max_length=4_000)]
    color: Color = "ink"

    @field_validator("path")
    @classmethod
    def svg_path_only(cls, value: str) -> str:
        if not _PATH.fullmatch(value):
            raise ValueError("stroke path must be SVG path data")
        return value


class Label(ContractModel):
    x: Annotated[float, Field(ge=0, le=2_000)]
    y: Annotated[float, Field(ge=0, le=2_000)]
    text: Annotated[str, Field(min_length=1, max_length=80)]
    color: Color = "ink"


class DrawStep(ContractModel):
    narration: Text
    strokes: Annotated[tuple[Stroke, ...], Field(min_length=1, max_length=12)]
    labels: Annotated[tuple[Label, ...], Field(max_length=12)] = ()


class DrawnExplanation(_Checkpoint):
    kind: Literal["drawn_explanation"] = "drawn_explanation"
    width: Annotated[int, Field(ge=200, le=2_000)] = 760
    height: Annotated[int, Field(ge=100, le=2_000)] = 320
    steps: Annotated[tuple[DrawStep, ...], Field(min_length=1, max_length=10)]
    draw_task: Text | None = None


Checkpoint = Annotated[
    Union[Explanation, Choice, Calculation, Estimate, DrawnExplanation], Field(discriminator="kind")
]


class SourceNote(ContractModel):
    ref: Annotated[str, Field(min_length=1, max_length=200)]
    title: ShortText


class LernsequenzV1(ContractModel):
    version: Literal["1"] = "1"
    sequence_id: SafeToken
    course_id: SafeToken
    title: ShortText
    kind: Literal["presentation", "video"]
    sections: Annotated[tuple[Section, ...], Field(min_length=1, max_length=80)]
    checkpoints: Annotated[tuple[Checkpoint, ...], Field(max_length=80)] = ()
    sources: Annotated[tuple[SourceNote, ...], Field(max_length=80)] = ()

    @model_validator(mode="after")
    def ids_resolve(self) -> "LernsequenzV1":
        sections = [section.section_id for section in self.sections]
        checkpoints = [checkpoint.checkpoint_id for checkpoint in self.checkpoints]
        if len(set(sections)) != len(sections) or len(set(checkpoints)) != len(checkpoints):
            raise ValueError("section and checkpoint ids must be unique")
        unknown = {c.after_section for c in self.checkpoints} - set(sections)
        if unknown:
            raise ValueError("checkpoints refer to unknown sections")
        return self

    def checkpoint(self, checkpoint_id: str) -> Checkpoint:
        for checkpoint in self.checkpoints:
            if checkpoint.checkpoint_id == checkpoint_id:
                return checkpoint
        raise LookupError("checkpoint is unknown")


# Calculation steps lose their "expected" formula separately.
_SECRET_FIELDS = {
    "choice": {"correct", "rationale"},
    "estimate": {"value", "rationale", "good_from", "good_to"},
}


def public_view(sequence: LernsequenzV1) -> dict[str, object]:
    """What the browser may see: everything except solutions and their explanations."""
    data = sequence.model_dump(mode="json")
    for checkpoint in data["checkpoints"]:
        hidden = _SECRET_FIELDS.get(checkpoint["kind"], set())
        if checkpoint["kind"] == "calculation":
            for step in checkpoint["steps"]:
                step.pop("expected", None)
        else:
            if checkpoint["kind"] == "choice":
                # Only whether several options are right (checkboxes vs. radio buttons).
                checkpoint["multiple"] = len(checkpoint["correct"]) > 1
            for name in hidden:
                checkpoint.pop(name, None)
    return data
