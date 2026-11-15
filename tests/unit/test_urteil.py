"""Die eine Regel, die aus einer Punktzahl ein Urteil macht."""

from __future__ import annotations

import pytest

from spaces.learning.services.evaluation.rubric import answer_outcome_code
from spaces.learning.services.evaluation.urteil import urteil


@pytest.mark.parametrize(
    ("score", "erwartet"),
    [(1.0, "correct"), (0.7, "correct"), (0.69, "partial"), (0.4, "partial"), (0.39, "incorrect"), (0.0, "incorrect")],
)
def test_rubrik_aufgaben_sind_ab_07_richtig(score: float, erwartet: str) -> None:
    assert urteil(score, rubrik=True) == erwartet


@pytest.mark.parametrize(
    ("score", "erwartet"),
    [(1.0, "correct"), (0.999, "correct"), (0.83, "partial"), (0.65, "partial"), (0.64, "incorrect"), (0.0, "incorrect")],
)
def test_feste_aufgaben_sind_nur_vollstaendig_richtig(score: float, erwartet: str) -> None:
    # Fuenf von sechs Paaren (0,83) bleibt "teilweise": eine feste Aufgabe ist richtig oder nicht.
    assert urteil(score, rubrik=False) == erwartet


def test_rubrik_code_folgt_derselben_regel() -> None:
    assert answer_outcome_code(0.7) == "answer_correct"
    assert answer_outcome_code(0.69) == "answer_partial"
    assert answer_outcome_code(0.39) == "answer_incorrect"
