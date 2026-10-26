"""Numbers the way people type them: German reading first, ambiguity flagged, reasons for refusals."""

from __future__ import annotations

import pytest

from spaces.learning.contracts.lernsequenz import LernsequenzV1
from spaces.learning.services.lernsequenz.bewertung import grade
from spaces.learning.services.lernsequenz.zahlen import read_number, why_not_a_number
from spaces.learning.tests.unit.test_lernsequenz import sequence_data


def seq() -> LernsequenzV1:
    data = sequence_data()
    data["checkpoints"].append({
        "kind": "estimate", "checkpoint_id": "schaetz", "after_section": "f2", "title": "Jahresnutzen",
        "question": "Wie viel Euro im Jahr?", "minimum": 0, "maximum": 10000, "step": 100, "unit": "€",
        "good_from": 3000, "good_to": 5000, "value": 4000, "rationale": "Rund 4.000 €.",
    })
    return LernsequenzV1.model_validate(data)


def seq_with_share() -> LernsequenzV1:
    data = sequence_data()
    data["checkpoints"][1]["steps"].append(
        {"step_id": "anteil", "title": "Anteil", "unit": "", "hint": "20 von 100", "expected": "0.2"}
    )
    return LernsequenzV1.model_validate(data)


def test_factor_1000_off_gets_a_separator_hint_not_a_wrong():
    result = grade(seq(), "roi", {"values": {"h": "1000", "eur": "45,000"}})
    step = result["steps"][1]
    assert step["correct"] is False and step["separator_hint"] is True
    plain_wrong = grade(seq(), "roi", {"values": {"h": "1000", "eur": "12"}})["steps"][1]
    assert plain_wrong["separator_hint"] is False


def test_percent_input_counts_as_share_when_expected_is_a_share():
    result = grade(seq_with_share(), "roi", {"values": {"anteil": "20 %"}})
    assert result["steps"][0]["correct"] is True


def test_estimate_gets_a_separator_hint():
    result = grade(seq(), "schaetz", {"value": "4,000"})
    assert result["inside_band"] is False and result["separator_hint"] is True
    assert grade(seq(), "schaetz", {"value": "4.000"})["inside_band"] is True


@pytest.mark.parametrize("text,value,ambiguous,alternative", [
    ("45.000", 45000.0, True, 45.0),
    ("45,000", 45.0, True, 45000.0),
    ("1,000", 1.0, True, 1000.0),
    ("45000", 45000.0, False, None),
    ("45.000,50", 45000.5, False, None),
    ("45,000.50", 45000.5, False, None),
    ("45 000", 45000.0, False, None),
    ("45,5", 45.5, False, None),
    ("45k", 45000.0, False, None),
    ("45 Tsd", 45000.0, False, None),
    ("1,2 Mio", 1_200_000.0, False, None),
    ("ca. 45.000", 45000.0, True, 45.0),
    ("45.000 €/Jahr", 45000.0, True, 45.0),
    ("-1.500", -1500.0, True, -1.5),
])
def test_read_number_reads_german_first_and_flags_ambiguity(text, value, ambiguous, alternative):
    reading = read_number(text)
    assert reading.value == value and reading.ambiguous is ambiguous and reading.alternative == alternative


def test_percent_and_display():
    assert read_number("20 %").percent is True and read_number("20 %").value == 20.0
    assert read_number("45000").display == "45.000" and read_number("1288.5").display == "1.288,5"


@pytest.mark.parametrize("text,reason", [("40-50", "spanne"), ("12.5.2026", "datum"), ("", "leer"), ("viel", "unlesbar")])
def test_unreadable_inputs_say_why(text, reason):
    assert read_number(text) is None and why_not_a_number(text) == reason
