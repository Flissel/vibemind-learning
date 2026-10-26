"""Server-side grading of sequence checkpoints; the browser never sees a solution first."""

from __future__ import annotations

import re
from typing import Any

from spaces.learning.contracts.lernsequenz import (
    Calculation,
    Choice,
    Cloze,
    CommandCheck,
    DrawnExplanation,
    Estimate,
    Hotspot,
    LernsequenzV1,
    Matching,
    Ordering,
    OutputRule,
    Profile,
    Step,
    TerminalSim,
    evaluate_formula,
)
from spaces.learning.services.lernsequenz.befehlsausgabe import inspect_output
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
    hands_on = {
        Ordering: _ordering, Cloze: _cloze, Matching: _matching, Hotspot: _hotspot,
        TerminalSim: _terminal_sim, Step: _step, Profile: _profile,
    }
    for kind, scorer in hands_on.items():
        if isinstance(checkpoint, kind):
            return scorer(checkpoint, answer)
    return {"kind": checkpoint.kind, "seen": True}


# --- hands-on checkpoints -------------------------------------------------------

# Copied commands arrive with dashes and quotes a word processor swapped in.
_ASCII = str.maketrans({"–": "--", "—": "--", "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"'})


def _plain(text: str) -> str:
    return " ".join(text.translate(_ASCII).split()).casefold()


def _ordering(checkpoint: Ordering, answer: dict[str, Any]) -> dict[str, Any]:
    order = answer.get("order")
    ids = [item.item_id for item in checkpoint.items]
    if not isinstance(order, list) or not all(isinstance(i, str) for i in order) or sorted(order) != sorted(ids):
        raise ValueError("order must contain each item exactly once")
    hits = sum(given == expected for given, expected in zip(order, checkpoint.correct))
    return {"kind": "ordering", "correct": hits == len(ids), "score": hits / len(ids),
            "correct_order": list(checkpoint.correct), "refs": list(checkpoint.refs)}


def _cloze(checkpoint: Cloze, answer: dict[str, Any]) -> dict[str, Any]:
    values = answer.get("values")
    if not isinstance(values, dict) or set(values) != {blank.blank_id for blank in checkpoint.blanks}:
        raise ValueError("a value per blank is required")
    blanks = []
    for blank in checkpoint.blanks:
        given = values[blank.blank_id]
        accepted = {_plain(text) for text in blank.accepted}
        blanks.append({"blank_id": blank.blank_id, "correct": isinstance(given, str) and _plain(given) in accepted})
    return {"kind": "cloze", "blanks": blanks, "score": sum(b["correct"] for b in blanks) / len(blanks),
            "refs": list(checkpoint.refs)}


def _matching(checkpoint: Matching, answer: dict[str, Any]) -> dict[str, Any]:
    pairs = answer.get("pairs")
    right = {item.item_id for item in checkpoint.right}
    if (not isinstance(pairs, dict) or set(pairs) != set(checkpoint.pairs)
            or not all(isinstance(v, str) and v in right for v in pairs.values())):
        raise ValueError("a partner per left item is required")
    hits = sum(pairs[left] == expected for left, expected in checkpoint.pairs.items())
    return {"kind": "matching", "correct": hits == len(checkpoint.pairs), "score": hits / len(checkpoint.pairs),
            "pairs": dict(checkpoint.pairs), "refs": list(checkpoint.refs)}


def _hotspot(checkpoint: Hotspot, answer: dict[str, Any]) -> dict[str, Any]:
    rect = answer.get("rect")
    try:
        x, y, w, h = (float(rect[key]) for key in ("x", "y", "w", "h"))
    except (TypeError, KeyError, ValueError) as error:
        raise ValueError("rect with x, y, w and h is required") from error
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > checkpoint.width or y + h > checkpoint.height:
        raise ValueError("rect must lie inside the image")
    target = checkpoint.target
    centre_x, centre_y = x + w / 2, y + h / 2
    inside = target.x <= centre_x <= target.x + target.w and target.y <= centre_y <= target.y + target.h
    # A frame around the whole screenshot is not an answer: at most four times the target.
    tight = w * h <= 4 * target.w * target.h
    hit = inside and tight
    return {"kind": "hotspot", "hit": hit, "score": 1.0 if hit else 0.0, "target": target.model_dump(mode="json"),
            "refs": list(checkpoint.refs)}


def _terminal_sim(checkpoint: TerminalSim, answer: dict[str, Any]) -> dict[str, Any]:
    command = answer.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 500:
        raise ValueError("command is required")
    typed = " ".join(command.translate(_ASCII).split())
    for sim in checkpoint.commands:
        if re.search(sim.pattern, typed):
            return {"kind": "terminal_sim", "output": sim.output, "reached": sim.reaches_goal,
                    "score": 1.0 if sim.reaches_goal else 0.0, "refs": list(checkpoint.refs)}
    return {"kind": "terminal_sim", "output": checkpoint.unknown_output, "reached": False, "score": 0.0,
            "refs": list(checkpoint.refs)}


def _step(checkpoint: Step, answer: dict[str, Any]) -> dict[str, Any]:
    state = answer.get("state")
    if state == "done":
        return {"kind": "step", "done": True, "score": 1.0, "refs": list(checkpoint.refs)}
    if state != "differs":
        raise ValueError("state must be done or differs")
    if checkpoint.check is not None:
        if "output" not in answer:
            raise ValueError("paste what the command printed")
        return _command(checkpoint.check, {"output": answer.get("output"), "shell": answer.get("shell")})
    return {"kind": "step", "done": False, "score": 0.0, "refs": list(checkpoint.refs),
            "message": "Sieht es bei Ihnen anders aus, fragen Sie den Tutor zu diesem Schritt – er kennt den "
                       "erwarteten Stand und die häufigen Ursachen."}


def _profile(checkpoint: Profile, answer: dict[str, Any]) -> dict[str, Any]:
    choices = answer.get("choices")
    allowed = {question.key: {option.option_id for option in question.options} for question in checkpoint.questions}
    if not isinstance(choices, dict) or not set(choices) <= set(allowed):
        raise ValueError("choices must answer the profile questions")
    if any(option not in allowed[key] for key, option in choices.items()):
        raise ValueError("unknown profile option")
    return {"kind": "profile", "saved": True, "choices": dict(choices), "refs": list(checkpoint.refs)}


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
    # The output itself never leaves this function (not echoed, not logged): it can hold keys.
    findings = inspect_output(output, [command.command for command in checkpoint.commands])
    base: dict[str, Any] = {"kind": "command_check", "refs": list(checkpoint.refs)}
    if findings.looks_like_secret:
        base["warning"] = "Die Ausgabe enthält etwas, das wie ein Schlüssel aussieht. Sie wird nicht gespeichert."
    if findings.is_the_command:
        return {**base, "recognized": True, "ok": False, "score": 0.0, "fix": [],
                "message": "Das ist der Befehl selbst, nicht seine Ausgabe. Führen Sie ihn aus und fügen Sie "
                           "ein, was das Terminal darunter ausgibt."}
    if findings.typographic:
        what = " und ".join(_TYPOGRAPHIC[name] for name in findings.typographic)
        return {**base, "recognized": True, "ok": False, "score": 0.0,
                "fix": [command.model_dump() for command in checkpoint.commands if command.shell == shell],
                "message": f"Der Befehl enthält {what}. Das passiert beim Kopieren aus Word, PDF oder Chat. "
                           "Kopieren Sie den Befehl mit dem Knopf hier oder tippen Sie ihn ab:"}
    for rule in checkpoint.problems:
        if _matches(rule, output, shell):
            fix = [command.model_dump() for command in rule.fix if command.shell == shell]
            return {**base, "recognized": True, "ok": False, "score": 0.0, "message": rule.message, "fix": fix}
    if _matches(checkpoint.success, output, shell):
        return {**base, "recognized": True, "ok": True, "score": 1.0, "message": checkpoint.success.message, "fix": []}
    return {**base, "recognized": False, "ok": False, "score": 0.0, "message": checkpoint.unknown_hint, "fix": []}


_TYPOGRAPHIC = {
    "gedankenstrich": "einen Gedankenstrich (–) statt zwei Bindestrichen (--)",
    "anfuehrungszeichen": "typografische Anführungszeichen statt gerader (\")",
}


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
