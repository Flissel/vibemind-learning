"""Abgabe einer Programmieraufgabe: Testfaelle fuer die Huelle, Testlauf in der Bewertung."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from spaces.learning.services.adaptive_engine.models import LearningResponse
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService, AnswerCommand
from spaces.learning.services.evaluation.code_tests import TestErgebnis
from spaces.learning.tests.unit.test_vorbewertung import ACTOR, NOW, _Rubrik, _rubric_item, _start, _store

TESTS = [{"name": "drei Zahlen", "eingabe": "3 4 5", "erwartet": "12"},
         {"name": "leer", "eingabe": "", "erwartet": "0"}]
ALLE = TestErgebnis(bestanden=2, gesamt=2, fehlgeschlagen=())
EINER = TestErgebnis(bestanden=1, gesamt=2, fehlgeschlagen=("leer",))


def _code_item(*, tests: list[dict[str, str]] | None = TESTS) -> dict[str, object]:
    spec = _rubric_item("code")
    config = dict(spec["scoring_config"])  # type: ignore[arg-type]
    config["presentation"] = {"sprache": "python", "startcode": "import sys\n"}
    if tests is not None:
        config["tests"] = tests
    return {**spec, "scoring_config": config}


def _dienst(tmp_path: Path, *, score: float = 1.0, tests: list[dict[str, str]] | None = TESTS):
    engine, factory, course_id, _ = _store(tmp_path, [_code_item(tests=tests), _code_item(tests=tests)])
    rubrik = _Rubrik(score=score)
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=rubrik)
    return engine, factory, service, rubrik, _start(service, course_id)


def _abgeben(service, turn, *, testlauf=None, answer=None):
    return service.answer(AnswerCommand(
        session_id=turn.session_id, actor_id=ACTOR, expected_session_revision=turn.session_revision,
        answer=answer or {"text": "print(12)"}, testlauf=testlauf,
    ))


def test_lernende_sehen_sprache_und_startcode_aber_nie_die_tests(tmp_path: Path) -> None:
    engine, _, _, _, turn = _dienst(tmp_path)
    assert turn.task.presentation == {"sprache": "python", "startcode": "import sys\n"}
    assert "drei Zahlen" not in turn.model_dump_json() and "erwartet" not in turn.model_dump_json()
    engine.dispose()


def test_bewertungsdaten_liefern_der_huelle_sprache_und_tests(tmp_path: Path) -> None:
    engine, _, service, _, turn = _dienst(tmp_path)
    daten = service.bewertungsdaten(turn.session_id, ACTOR, turn.session_revision)
    assert daten.art == "code" and daten.item_type == "code"
    assert daten.sprache == "python"
    assert [t.model_dump() for t in daten.tests] == TESTS
    # Die Tests laufen am PC; Musterloesung und Kriterien braucht dort niemand.
    assert daten.musterloesung is None and daten.kriterien == []
    engine.dispose()


def test_bewertungsdaten_fuer_aeltere_code_aufgaben_ohne_tests(tmp_path: Path) -> None:
    engine, factory, service, _, turn = _dienst(tmp_path, tests=None)
    daten = service.bewertungsdaten(turn.session_id, ACTOR, turn.session_revision)
    assert daten.art == "code" and daten.tests == []
    engine.dispose()


def test_alle_tests_bestanden_bleibt_richtig_und_der_lauf_geht_an_die_bewertung(tmp_path: Path) -> None:
    engine, factory, service, rubrik, turn = _dienst(tmp_path)
    nach = _abgeben(service, turn, testlauf=ALLE)
    assert nach.feedback.state == "correct" and nach.feedback.score == 1.0
    anfrage, _ = rubrik.calls[0]
    assert anfrage.response == {"text": "print(12)", "testlauf": {"bestanden": 2, "gesamt": 2, "fehlgeschlagen": []}}
    with factory() as db:
        gespeichert = db.scalars(select(LearningResponse)).one().answer
    assert gespeichert["testlauf"] == {"bestanden": 2, "gesamt": 2, "fehlgeschlagen": []}
    engine.dispose()


def test_gescheiterte_tests_deckeln_auf_teilweise(tmp_path: Path) -> None:
    # Die Rubrik gibt volle Punkte, aber ein Test scheitert: hoechstens teilweise.
    engine, _, service, rubrik, turn = _dienst(tmp_path)
    nach = _abgeben(service, turn, testlauf=EINER)
    assert nach.feedback.state == "partial" and nach.feedback.score < 0.7
    assert "tests_nicht_bestanden" in nach.feedback.rationale_codes
    assert "answer_correct" not in nach.feedback.rationale_codes
    assert rubrik.calls[0][0].response["testlauf"]["fehlgeschlagen"] == ["leer"]
    engine.dispose()


def test_schlechte_rubrik_bleibt_schlecht_trotz_bestandener_tests(tmp_path: Path) -> None:
    engine, _, service, _, turn = _dienst(tmp_path, score=0.2)
    nach = _abgeben(service, turn, testlauf=ALLE)
    assert nach.feedback.state == "incorrect" and nach.feedback.score == pytest.approx(0.2)
    assert "tests_nicht_bestanden" not in nach.feedback.rationale_codes
    engine.dispose()


@pytest.mark.parametrize("testlauf", [None, TestErgebnis(bestanden=1, gesamt=1, fehlgeschlagen=())])
def test_ohne_passenden_testlauf_wird_gedeckelt(tmp_path: Path, testlauf) -> None:
    # Kein Lauf oder einer mit falscher Testzahl (nicht diese Aufgabe): zaehlt als nicht gelaufen.
    engine, _, service, rubrik, turn = _dienst(tmp_path)
    nach = _abgeben(service, turn, testlauf=testlauf)
    assert nach.feedback.state == "partial"
    assert rubrik.calls[0][0].response["testlauf"] == {"gelaufen": False}
    engine.dispose()


def test_ein_testlauf_in_der_antwort_selbst_zaehlt_nicht(tmp_path: Path) -> None:
    # Was im Antwortfeld steht, kann aus dem Browser kommen: nur das Feld des Befehls gilt.
    engine, _, service, rubrik, turn = _dienst(tmp_path)
    gefaelscht = {"text": "print(12)", "testlauf": {"bestanden": 2, "gesamt": 2, "fehlgeschlagen": []}}
    nach = _abgeben(service, turn, answer=gefaelscht)
    assert nach.feedback.state == "partial"
    assert rubrik.calls[0][0].response["testlauf"] == {"gelaufen": False}
    engine.dispose()


def test_code_aufgabe_ohne_tests_bleibt_wie_bisher(tmp_path: Path) -> None:
    engine, _, service, rubrik, turn = _dienst(tmp_path, tests=None)
    nach = _abgeben(service, turn)
    assert nach.feedback.state == "correct"
    assert rubrik.calls[0][0].response == {"text": "print(12)"}
    engine.dispose()
