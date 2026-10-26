"""Numbers the way people type them: German reading first, ambiguity flagged, reasons for refusals."""

from __future__ import annotations

import pytest

from spaces.learning.services.lernsequenz.zahlen import read_number, why_not_a_number


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
