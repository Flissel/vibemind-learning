"""Learning sequences: contract, public view without solutions, grading, storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from spaces.learning.contracts.lernsequenz import LernsequenzV1, evaluate_formula, public_view
from spaces.learning.services.lernsequenz.bewertung import grade, parse_number
from spaces.learning.services.lernsequenz.store import SequenceStore


def sequence_data() -> dict:
    return {
        "sequence_id": "seq-roi",
        "course_id": "b189a3ec-a82a-4ae2-8cab-e71ab0d75055",
        "title": "KI im Rechnungseingang",
        "kind": "presentation",
        "sections": [
            {"kind": "slide", "section_id": "f1", "title": "Warum jetzt", "points": ["Zeit", "Qualität"],
             "narration": "Drei Hebel.", "refs": ["c1"]},
            {"kind": "slide", "section_id": "f2", "title": "Ablauf", "narration": "So läuft es.", "refs": ["c1"]},
        ],
        "checkpoints": [
            {"kind": "choice", "checkpoint_id": "q1", "after_section": "f1", "title": "Erste Frage",
             "question": "Was zuerst?", "options": [
                 {"option_id": "a", "text": "Modell"}, {"option_id": "b", "text": "Problem und Daten"}],
             "correct": ["b"], "rationale": "Erst das Problem.", "refs": ["c1"]},
            {"kind": "calculation", "checkpoint_id": "roi", "after_section": "f2", "title": "ROI",
             "task": "2 × 20 h × 46 Wochen, 70 % gespart, 45 €/h, 18.000 € Kosten.",
             "steps": [
                 {"step_id": "h", "title": "Stunden", "unit": "h", "hint": "2*20*46*0,7",
                  "expected": "2*20*46*0.7"},
                 {"step_id": "eur", "title": "Euro", "unit": "€", "hint": "mal 45",
                  "expected": "s1*45"},
                 {"step_id": "roi", "title": "ROI", "unit": "%", "hint": "(E-K)/K",
                  "expected": "(s2-18000)/18000*100"},
             ]},
            {"kind": "estimate", "checkpoint_id": "est", "after_section": "f2", "title": "Schätzen",
             "question": "Anteil ohne Mensch?", "minimum": 0, "maximum": 100, "step": 5, "unit": "%",
             "good_from": 70, "good_to": 90, "value": 80, "rationale": "Laut Aufnahme rund 80 %."},
            {"kind": "drawn_explanation", "checkpoint_id": "tafel", "after_section": "f2",
             "title": "Tafelbild", "steps": [
                 {"narration": "Rechnung kommt rein.", "strokes": [{"path": "M40 120 h150 v90 h-150 z"}],
                  "labels": [{"x": 115, "y": 172, "text": "Rechnung"}]}],
             "draw_task": "Zeichnen Sie die Kontrolle ein."},
        ],
    }


@pytest.fixture()
def sequence() -> LernsequenzV1:
    return LernsequenzV1.model_validate(sequence_data())


def test_public_view_carries_no_solutions(sequence) -> None:
    text = json.dumps(public_view(sequence))
    for secret in ('"correct"', "Erst das Problem", "s1*45", "2*20*46*0.7", '"value"', "rund 80"):
        assert secret not in text
    assert "Problem und Daten" in text and "Was zuerst?" in text


def test_choice_is_graded_on_the_server(sequence) -> None:
    right = grade(sequence, "q1", {"selected": ["b"]})
    wrong = grade(sequence, "q1", {"selected": ["a"]})
    assert right["correct"] and right["rationale"] == "Erst das Problem."
    assert not wrong["correct"] and wrong["correct_options"] == ["b"]
    with pytest.raises(ValueError):
        grade(sequence, "q1", {"selected": ["zzz"]})


def test_calculation_counts_follow_up_errors_once(sequence) -> None:
    exact = grade(sequence, "roi", {"values": {"h": "1.288", "eur": "57.960", "roi": "222"}})
    assert [s["correct"] for s in exact["steps"]] == [True, True, True] and exact["score"] == 1.0

    # Step 1 wrong (1000 h); steps 2 and 3 correctly computed from it.
    follow = grade(sequence, "roi", {"values": {"h": "1000", "eur": "45000", "roi": "150"}})
    assert [s["correct"] for s in follow["steps"]] == [False, True, True]
    assert [s["follow_up"] for s in follow["steps"]] == [False, True, True]

    single = grade(sequence, "roi", {"values": {"h": "1288,0"}})
    assert single["steps"] == [
        {"step_id": "h", "readable": True, "correct": True, "follow_up": False, "separator_hint": False}
    ]


def test_estimate_reveals_band_after_answer(sequence) -> None:
    result = grade(sequence, "est", {"value": 75})
    assert result["inside_band"] and result["value"] == 80
    assert not grade(sequence, "est", {"value": 40})["inside_band"]
    with pytest.raises(ValueError):
        grade(sequence, "est", {"value": 140})


@pytest.mark.parametrize("text,value", [
    ("57.960", 57960), ("57.960,5", 57960.5), ("57,960.5", 57960.5), ("1288,5", 1288.5),
    ("222 %", 222), ("1288 h", 1288), ("12.5", 12.5), ("abc", None), ("", None),
])
def test_numbers_are_read_like_people_type_them(text, value) -> None:
    assert parse_number(text) == value


def test_formulas_cannot_run_code() -> None:
    assert evaluate_formula("(s1-2)*3", {"s1": 4}) == 6
    for bad in ("__import__('os')", "s1.real", "2**8", "abs(s1)"):
        with pytest.raises((ValueError, SyntaxError)):
            evaluate_formula(bad, {"s1": 1})
    data = sequence_data()
    data["checkpoints"][1]["steps"][0]["expected"] = "s1*2"
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)


def test_drawing_paths_and_ids_are_checked() -> None:
    data = sequence_data()
    data["checkpoints"][3]["steps"][0]["strokes"][0]["path"] = "M0 0 <script>"
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)
    data = sequence_data()
    data["checkpoints"][0]["after_section"] = "missing"
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)


def test_store_round_trip_and_sketches(tmp_path: Path, sequence) -> None:
    store = SequenceStore(tmp_path)
    store.save(sequence)
    assert store.load("seq-roi") == sequence
    with pytest.raises(LookupError):
        store.load("../etc")
    sketch = store.save_sketch(sequence, "tafel", [[[1, 2], [3, 4]]])
    saved = tmp_path / "lernsequenzen" / "skizzen" / "seq-roi" / "tafel" / f"{sketch}.json"
    assert json.loads(saved.read_text())["strokes"] == [[[1, 2], [3, 4]]]
    with pytest.raises(ValueError):
        store.save_sketch(sequence, "q1", [[[1, 2]]])
    with pytest.raises(ValueError):
        store.save_sketch(sequence, "tafel", [[["x", 2]]])
