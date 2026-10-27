"""Learning sequences: a presentation or video with checkpoints, played by Learning itself.

A sequence is an ordered list of sections (slides or video segments) and checkpoints that
pause it: an explanation, a choice question, a calculation with steps, an estimate or an
explanation drawn stroke by stroke. LearnHouse pages embed the player; the player only
ever receives ``public_view`` (no solutions), answers are graded on the server.
"""

from __future__ import annotations

import ast
import hashlib
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


class Condition(ContractModel):
    """Shown only when the person's profile answer for ``key`` is ``option`` ("Dein Weg")."""

    key: SafeToken
    option: SafeToken


Conditions = Annotated[tuple[Condition, ...], Field(max_length=4)]


# --- Sections -----------------------------------------------------------------

class Slide(ContractModel):
    kind: Literal["slide"] = "slide"
    section_id: SafeToken
    title: ShortText
    statement: Text | None = None
    points: Annotated[tuple[ShortText, ...], Field(max_length=8)] = ()
    narration: Text
    refs: Refs = ()
    only_for: Conditions = ()


class VideoSegment(ContractModel):
    kind: Literal["video"] = "video"
    section_id: SafeToken
    title: ShortText
    url: Annotated[str, Field(min_length=8, max_length=1_000)]
    start_s: Annotated[float, Field(ge=0)] = 0
    end_s: Annotated[float, Field(gt=0)] | None = None
    transcript: Text | None = None
    refs: Refs = ()
    only_for: Conditions = ()

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
    only_for: Conditions = ()


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
    # The factory activity this question comes from: its practice item gets the answer
    # (learner mastery), scored by the engine itself.
    activity_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None

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


Shell = Literal["powershell", "cmd", "unix"]


class ShellCommand(ContractModel):
    shell: Shell
    command: Annotated[str, Field(min_length=1, max_length=500)]


class OutputRule(ContractModel):
    """A recognisable terminal output (regex, case-insensitive) and what it means."""

    pattern: Annotated[str, Field(min_length=1, max_length=300)]
    message: Text
    shells: Annotated[tuple[Shell, ...], Field(max_length=3)] = ()
    fix: Annotated[tuple[ShellCommand, ...], Field(max_length=6)] = ()

    @field_validator("pattern")
    @classmethod
    def compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class CommandCheck(_Checkpoint):
    """Run a command in your own terminal and paste what it printed: the output is
    recognised by fixed rules (no model), a known error gets its fix for that shell."""

    kind: Literal["command_check"] = "command_check"
    task: Text
    commands: Annotated[tuple[ShellCommand, ...], Field(min_length=1, max_length=3)]
    success: OutputRule
    problems: Annotated[tuple[OutputRule, ...], Field(max_length=12)] = ()
    unknown_hint: Text

    @model_validator(mode="after")
    def one_command_per_shell(self) -> "CommandCheck":
        shells = [command.shell for command in self.commands]
        if len(set(shells)) != len(shells):
            raise ValueError("one command per shell")
        return self


# --- Hands-on checkpoints -------------------------------------------------------
# The person works along on their own computer; everything here is graded by rules.

_MEDIA_PATH = r"^/lernsequenz-medien/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(png|jpg|webp)$"
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9][A-Za-z0-9._:-]*)\s*\}\}")


def _unique(values: list[str], what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must be unique")


class OrderItem(ContractModel):
    item_id: SafeToken
    text: ShortText


class Ordering(_Checkpoint):
    """Put the steps in the right order. Answer: ``{"order": [item_id, ...]}``."""

    kind: Literal["ordering"] = "ordering"
    task: Text
    items: Annotated[tuple[OrderItem, ...], Field(min_length=3, max_length=8)]
    correct: Annotated[tuple[SafeToken, ...], Field(min_length=3, max_length=8)]

    @model_validator(mode="after")
    def correct_is_a_permutation(self) -> "Ordering":
        ids = [item.item_id for item in self.items]
        _unique(ids, "ordering item ids")
        if sorted(self.correct) != sorted(ids):
            raise ValueError("ordering correct must be a permutation of the item ids")
        return self


class Blank(ContractModel):
    blank_id: SafeToken
    accepted: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=200)], ...], Field(min_length=1, max_length=6)
    ]


class Cloze(_Checkpoint):
    """A command with blanks (``irm https://claude.ai/{{datei}} | iex``). Answer: ``{"values": {blank_id: text}}``."""

    kind: Literal["cloze"] = "cloze"
    task: Text
    template: Annotated[str, Field(min_length=1, max_length=500)]
    blanks: Annotated[tuple[Blank, ...], Field(min_length=1, max_length=6)]
    shell: Shell | None = None

    @model_validator(mode="after")
    def placeholders_have_blanks(self) -> "Cloze":
        ids = [blank.blank_id for blank in self.blanks]
        _unique(ids, "cloze blank ids")
        if set(_PLACEHOLDER.findall(self.template)) != set(ids):
            raise ValueError("cloze placeholders and blanks must match")
        return self


class MatchItem(ContractModel):
    item_id: SafeToken
    text: ShortText
    # A drawn window (or other picture) shown instead of / above the text; its size is
    # needed to lay it out before it loads.
    image: Annotated[str, Field(pattern=_MEDIA_PATH)] | None = None
    width: Annotated[int, Field(ge=50, le=4_000)] | None = None
    height: Annotated[int, Field(ge=50, le=4_000)] | None = None

    @model_validator(mode="after")
    def image_has_a_size(self) -> "MatchItem":
        if self.image is not None and (self.width is None or self.height is None):
            raise ValueError("a match item image needs width and height")
        return self


class Matching(_Checkpoint):
    """Match left to right. Answer: ``{"pairs": {left_id: right_id}}``."""

    kind: Literal["matching"] = "matching"
    task: Text
    left: Annotated[tuple[MatchItem, ...], Field(min_length=3, max_length=8)]
    right: Annotated[tuple[MatchItem, ...], Field(min_length=3, max_length=8)]
    pairs: dict[SafeToken, SafeToken]

    @model_validator(mode="after")
    def pairs_reference_items(self) -> "Matching":
        left = [item.item_id for item in self.left]
        right = [item.item_id for item in self.right]
        _unique(left, "matching left ids")
        _unique(right, "matching right ids")
        if set(self.pairs) != set(left) or not set(self.pairs.values()) <= set(right):
            raise ValueError("matching pairs must map every left item to a right item")
        return self


class Rect(ContractModel):
    x: Annotated[float, Field(ge=0)]
    y: Annotated[float, Field(ge=0)]
    w: Annotated[float, Field(gt=0)]
    h: Annotated[float, Field(gt=0)]


class ImageBlank(ContractModel):
    blank_id: SafeToken
    accepted: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=200)], ...], Field(min_length=1, max_length=6)
    ]
    rect: Rect


class ImageCloze(_Checkpoint):
    """Fill the empty boxes of a drawn terminal window. Answer: ``{"values": {blank_id: text}}``."""

    kind: Literal["image_cloze"] = "image_cloze"
    task: Text
    image: Annotated[str, Field(pattern=_MEDIA_PATH)]
    width: Annotated[int, Field(ge=50, le=4_000)]
    height: Annotated[int, Field(ge=50, le=4_000)]
    blanks: Annotated[tuple[ImageBlank, ...], Field(min_length=1, max_length=6)]
    shell: Shell

    @model_validator(mode="after")
    def boxes_inside_the_image(self) -> "ImageCloze":
        _unique([blank.blank_id for blank in self.blanks], "image cloze blank ids")
        for blank in self.blanks:
            r = blank.rect
            if r.x + r.w > self.width or r.y + r.h > self.height:
                raise ValueError("image cloze boxes must lie inside the image")
        return self


class Hotspot(_Checkpoint):
    """Mark the spot on an image with the frame. Answer: ``{"rect": {x, y, w, h}}`` in image pixels."""

    kind: Literal["hotspot"] = "hotspot"
    task: Text
    image: Annotated[str, Field(pattern=_MEDIA_PATH)]
    width: Annotated[int, Field(ge=100, le=4_000)]
    height: Annotated[int, Field(ge=100, le=4_000)]
    target: Rect

    @model_validator(mode="after")
    def target_inside_image(self) -> "Hotspot":
        if self.target.x + self.target.w > self.width or self.target.y + self.target.h > self.height:
            raise ValueError("hotspot target must lie inside the image")
        return self


class SimCommand(ContractModel):
    pattern: Annotated[str, Field(min_length=1, max_length=200)]
    output: Text
    reaches_goal: bool = False

    @field_validator("pattern")
    @classmethod
    def compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class TerminalSim(_Checkpoint):
    """A simulated terminal with the documented outputs. Answer: ``{"command": text}``."""

    kind: Literal["terminal_sim"] = "terminal_sim"
    task: Text
    shell: Shell
    prompt: ShortText
    commands: Annotated[tuple[SimCommand, ...], Field(min_length=1, max_length=20)]
    unknown_output: Text


class Step(_Checkpoint):
    """Do this on your own computer. Answer: ``{"state": "done"}`` or
    ``{"state": "differs", "shell": ..., "output": ...}`` (graded by ``check`` when present)."""

    kind: Literal["step"] = "step"
    instruction: Text
    commands: Annotated[tuple[ShellCommand, ...], Field(max_length=3)] = ()
    expected_text: Text | None = None
    expected_image: Annotated[str, Field(pattern=_MEDIA_PATH)] | None = None
    check: CommandCheck | None = None

    @model_validator(mode="after")
    def one_command_per_shell(self) -> "Step":
        _unique([command.shell for command in self.commands], "step command shells")
        return self


class ProfileOption(ContractModel):
    option_id: SafeToken
    text: ShortText


class ProfileQuestion(ContractModel):
    key: SafeToken
    question: ShortText
    options: Annotated[tuple[ProfileOption, ...], Field(min_length=2, max_length=6)]

    @model_validator(mode="after")
    def options_unique(self) -> "ProfileQuestion":
        _unique([option.option_id for option in self.options], "profile option ids")
        return self


class Profile(_Checkpoint):
    """"Dein Weg": the answers filter sections and checkpoints by ``only_for``. Answer: ``{"choices": {key: option_id}}``."""

    kind: Literal["profile"] = "profile"
    questions: Annotated[tuple[ProfileQuestion, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def keys_unique(self) -> "Profile":
        _unique([question.key for question in self.questions], "profile keys")
        return self


Checkpoint = Annotated[
    Union[
        Explanation, Choice, Calculation, Estimate, DrawnExplanation, CommandCheck,
        Ordering, Cloze, ImageCloze, Matching, Hotspot, TerminalSim, Step, Profile,
    ],
    Field(discriminator="kind"),
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
    # The recognition rules and their fixes come back with the graded output.
    "command_check": {"success", "problems"},
    "ordering": {"correct"},
    "cloze": {"blanks"},
    "matching": {"pairs"},
    "hotspot": {"target"},
    "terminal_sim": {"commands", "unknown_output"},
    "step": {"check"},
}


def _fixed_shuffle(items: list[dict], salt: str) -> list[dict]:
    """The same scrambled order on every load, so the authored order never shows and a reload
    does not hand the learner a second arrangement to compare against."""
    return sorted(items, key=lambda item: hashlib.sha1(f"{salt}:{item['item_id']}".encode()).hexdigest())


def public_view(sequence: LernsequenzV1) -> dict[str, object]:
    """What the browser may see: everything except solutions and their explanations."""
    data = sequence.model_dump(mode="json")
    for checkpoint in data["checkpoints"]:
        hidden = _SECRET_FIELDS.get(checkpoint["kind"], set())
        salt = f"{data['sequence_id']}:{checkpoint['checkpoint_id']}"
        if checkpoint["kind"] == "calculation":
            for step in checkpoint["steps"]:
                step.pop("expected", None)
        else:
            if checkpoint["kind"] == "choice":
                # Only whether several options are right (checkboxes vs. radio buttons).
                checkpoint["multiple"] = len(checkpoint["correct"]) > 1
            elif checkpoint["kind"] == "cloze":
                checkpoint["blank_ids"] = [blank["blank_id"] for blank in checkpoint["blanks"]]
            elif checkpoint["kind"] == "image_cloze":
                # Where the boxes are, never what goes in them.
                checkpoint["blanks"] = [{"blank_id": b["blank_id"], "rect": b["rect"]} for b in checkpoint["blanks"]]
            elif checkpoint["kind"] == "ordering":
                checkpoint["items"] = _fixed_shuffle(checkpoint["items"], salt)
            elif checkpoint["kind"] == "matching":
                checkpoint["right"] = _fixed_shuffle(checkpoint["right"], salt)
            elif checkpoint["kind"] == "step":
                checkpoint["has_check"] = checkpoint.get("check") is not None
            for name in hidden:
                checkpoint.pop(name, None)
    return data
