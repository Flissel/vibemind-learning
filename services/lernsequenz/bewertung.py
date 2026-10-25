"""Server-side grading of sequence checkpoints; the browser never sees a solution first."""

from __future__ import annotations

import re
from typing import Any

from spaces.learning.contracts.lernsequenz import (
    Calculation,
    Choice,
    DrawnExplanation,
    Estimate,
    LernsequenzV1,
    evaluate_formula,
)

_THOUSANDS = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def parse_number(text: object) -> float | None:
    """Read 57.960 / 57,960.5 / 1288,5 / '222 %' the way people type them."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"[\s€%]|h$|EUR", "", text.strip(), flags=re.IGNORECASE)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        decimal = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        cleaned = cleaned.replace(thousands, "").replace(decimal, ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif _THOUSANDS.fullmatch(cleaned):
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _within(value: float, expected: float, tolerance_percent: float) -> bool:
    return abs(value - expected) <= max(abs(expected) * tolerance_percent / 100, 1e-9)


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
    return {"kind": checkpoint.kind, "seen": True}


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
        given = parse_number(values.get(step.step_id))
        if given is not None:
            learner[name] = given
        if step.step_id not in values:
            continue
        ok = given is not None and _within(given, expected_own, checkpoint.tolerance_percent)
        follow_up = ok and not _within(expected_own, expected_true, checkpoint.tolerance_percent)
        results.append({
            "step_id": step.step_id,
            "readable": given is not None,
            "correct": ok,
            "follow_up": follow_up,
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
    return {
        "kind": "estimate",
        "inside_band": inside,
        "score": 1.0 if inside else 0.0,
        "value": checkpoint.value,
        "good_from": checkpoint.good_from,
        "good_to": checkpoint.good_to,
        "rationale": checkpoint.rationale,
        "refs": list(checkpoint.refs),
    }
