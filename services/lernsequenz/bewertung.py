"""Server-side grading of sequence checkpoints; the browser never sees a solution first."""

from __future__ import annotations

import re
from typing import Any

from spaces.learning.contracts.lernsequenz import (
    Calculation,
    Choice,
    CommandCheck,
    DrawnExplanation,
    Estimate,
    LernsequenzV1,
    OutputRule,
    evaluate_formula,
)
from spaces.learning.services.lernsequenz.zahlen import read_number

def parse_number(text: object) -> float | None:
    """Read 57.960 / 57,960.5 / 1288,5 / '222 %' the way people type them (German first)."""
    reading = read_number(text)
    return reading.value if reading else None


def _within(value: float, expected: float, tolerance_percent: float) -> bool:
    return abs(value - expected) <= max(abs(expected) * tolerance_percent / 100, 1e-9)


def _off_by_a_thousand(value: float, expected: float, tolerance_percent: float) -> bool:
    """45,000 read as 45 for an expected 45.000: the separator was meant the other way."""
    return _within(value * 1000, expected, tolerance_percent) or _within(value / 1000, expected, tolerance_percent)


def grade(sequence: LernsequenzV1, checkpoint_id: str, answer: dict[str, Any]) -> dict[str, Any]:
    checkpoint = sequence.checkpoint(checkpoint_id)
    if isinstance(checkpoint, Choice):
        return _choice(checkpoint, answer)
    if isinstance(checkpoint, Calculation):
        return _calculation(checkpoint, answer)
    if isinstance(checkpoint, Estimate):
        return _estimate(checkpoint, answer)
    if isinstance(checkpoint, DrawnExplanation):
        raise ValueError("drawings are saved, not graded here")
    if isinstance(checkpoint, CommandCheck):
        return _command(checkpoint, answer)
    return {"kind": checkpoint.kind, "seen": True}


_MAX_OUTPUT = 20_000


def _matches(rule: OutputRule, output: str, shell: str) -> bool:
    return (not rule.shells or shell in rule.shells) and re.search(rule.pattern, output, re.IGNORECASE) is not None


def _command(checkpoint: CommandCheck, answer: dict[str, Any]) -> dict[str, Any]:
    """Known errors first (an error text can contain a version number), then success."""
    output, shell = answer.get("output"), answer.get("shell")
    if not isinstance(output, str) or not output.strip():
        raise ValueError("paste what the command printed")
    if shell not in {command.shell for command in checkpoint.commands}:
        raise ValueError("unknown shell")
    output = output[:_MAX_OUTPUT]
    for rule in checkpoint.problems:
        if _matches(rule, output, shell):
            fix = [command.model_dump() for command in rule.fix if command.shell == shell]
            return {"kind": "command_check", "recognized": True, "ok": False, "score": 0.0,
                    "message": rule.message, "fix": fix, "refs": list(checkpoint.refs)}
    if _matches(checkpoint.success, output, shell):
        return {"kind": "command_check", "recognized": True, "ok": True, "score": 1.0,
                "message": checkpoint.success.message, "fix": [], "refs": list(checkpoint.refs)}
    return {"kind": "command_check", "recognized": False, "ok": False, "score": 0.0,
            "message": checkpoint.unknown_hint, "fix": [], "refs": list(checkpoint.refs)}


def _choice(checkpoint: Choice, answer: dict[str, Any]) -> dict[str, Any]:
    selected = answer.get("selected")
    if not isinstance(selected, list) or not selected or not all(isinstance(s, str) for s in selected):
        raise ValueError("choose at least one option")
    known = {option.option_id for option in checkpoint.options}
    if not set(selected) <= known:
        raise ValueError("unknown option")
    correct = set(selected) == set(checkpoint.correct)
    return {
        "kind": "choice",
        "correct": correct,
        "score": 1.0 if correct else 0.0,
        "correct_options": list(checkpoint.correct),
        "rationale": checkpoint.rationale,
        "refs": list(checkpoint.refs),
    }


def _calculation(checkpoint: Calculation, answer: dict[str, Any]) -> dict[str, Any]:
    values = answer.get("values")
    if not isinstance(values, dict):
        raise ValueError("values per step are required")
    learner: dict[str, float] = {}
    truth: dict[str, float] = {}
    results = []
    for index, step in enumerate(checkpoint.steps, start=1):
        name = f"s{index}"
        expected_true = evaluate_formula(step.expected, truth)
        truth[name] = expected_true
        # Follow-up errors count once: later steps are checked against the learner's own
        # earlier results (or the true ones where the learner gave none).
        basis = {key: learner.get(key, truth[key]) for key in truth if key != name}
        expected_own = evaluate_formula(step.expected, basis)
        tolerance = checkpoint.tolerance_percent
        reading = read_number(values.get(step.step_id))
        given = reading.value if reading is not None else None
        if reading is not None and reading.percent and abs(expected_own) < 1 and not _within(given, expected_own, tolerance):
            # "20 %" for an expected share of 0.2.
            given = given / 100
        if given is not None:
            learner[name] = given
        if step.step_id not in values:
            continue
        ok = given is not None and _within(given, expected_own, tolerance)
        follow_up = ok and not _within(expected_own, expected_true, tolerance)
        results.append({
            "step_id": step.step_id,
            "readable": given is not None,
            "correct": ok,
            "follow_up": follow_up,
            "separator_hint": not ok and given is not None and _off_by_a_thousand(given, expected_own, tolerance),
        })
    if not results:
        raise ValueError("no step answered")
    score = sum(result["correct"] for result in results) / len(checkpoint.steps)
    return {"kind": "calculation", "steps": results, "score": score, "refs": list(checkpoint.refs)}


def _estimate(checkpoint: Estimate, answer: dict[str, Any]) -> dict[str, Any]:
    value = parse_number(answer.get("value"))
    if value is None or not checkpoint.minimum <= value <= checkpoint.maximum:
        raise ValueError("estimate must be a number inside the range")
    inside = checkpoint.good_from <= value <= checkpoint.good_to
    in_band = lambda v: checkpoint.good_from <= v <= checkpoint.good_to  # noqa: E731
    return {
        "kind": "estimate",
        "inside_band": inside,
        "score": 1.0 if inside else 0.0,
        "separator_hint": not inside and (in_band(value * 1000) or in_band(value / 1000)),
        "value": checkpoint.value,
        "good_from": checkpoint.good_from,
        "good_to": checkpoint.good_to,
        "rationale": checkpoint.rationale,
        "refs": list(checkpoint.refs),
    }
