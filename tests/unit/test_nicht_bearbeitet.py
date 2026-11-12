"""Leere Antwort heisst "nicht bearbeitet": 0 Punkte, sofort gebucht, ohne Modell."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from spaces.learning.services.adaptive_engine.models import (
    LearningAttempt,
    LearningEvaluation,
)
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    AnswerCommand,
)
from spaces.learning.tests.unit.test_feste_liste import (
    ACTOR,
    NOW,
    _service,
    _start as _start_liste,
    _store as _store_liste,
    _versuche,
)
from spaces.learning.tests.unit.test_vorbewertung import (
    _rubric_item,
    _start,
    _store,
)


class _ExplodierendeRubrik:
    def evaluate(self, request, *, bild_png=None):
        raise AssertionError("Der Bewerter darf bei leerer Antwort nicht laufen.")


def _bewertungen(factory) -> list[LearningEvaluation]:
    with factory() as db:
        return list(db.scalars(select(LearningEvaluation)))


def test_leere_antwort_rubrik_ohne_modell(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("open")])
    service = AdaptiveSessionService(
        factory, clock=lambda: NOW, rubric_evaluator=_ExplodierendeRubrik()
    )
    turn = _start(service, course_id)

    ergebnis = service.answer(
        AnswerCommand(
            session_id=turn.session_id, actor_id=ACTOR,
            expected_session_revision=turn.session_revision, answer={},
        )
    )

    # Gebucht wie jede andere Antwort (Revision steigt; die Engine zaehlt Antwort und Folgeaufgabe).
    assert ergebnis.session_revision > turn.session_revision
    assert ergebnis.audit.mastery_applied is True
    bewertung = _bewertungen(factory)[0]
    assert bewertung.score == 0.0
    assert bewertung.accepted is True
    assert tuple(bewertung.rationale_codes) == ("nicht_bearbeitet",)
    assert bewertung.evaluator_type == "rubric"
    assert dict(bewertung.evaluator_versions) == {"scorer": "leer-v1", "grader": "server"}
    engine.dispose()


def test_leere_antwort_deterministisch(tmp_path: Path) -> None:
    factory, course_id = _store_liste(tmp_path, [1])
    service = _service(factory)
    turn = _start_liste(service, course_id, ablauf=None)

    service.answer(
        AnswerCommand(
            session_id=turn.session_id, actor_id=ACTOR,
            expected_session_revision=turn.session_revision, answer={},
        )
    )

    bewertung = _bewertungen(factory)[0]
    assert bewertung.score == 0.0
    assert tuple(bewertung.rationale_codes) == ("nicht_bearbeitet",)
    assert bewertung.evaluator_type == "deterministic"
    assert dict(bewertung.evaluator_versions) == {"scorer": "leer-v1", "grader": "server"}


def test_leere_antwort_schliesst_listenkurs(tmp_path: Path) -> None:
    factory, course_id = _store_liste(tmp_path, [1, 2])
    service = _service(factory)
    turn = _start_liste(service, course_id)

    ergebnis = None
    for versuch in _versuche(factory, turn):
        ergebnis = service.answer(
            AnswerCommand(
                session_id=turn.session_id, actor_id=ACTOR, expected_session_revision=1,
                attempt_id=versuch.id, answer={},
            )
        )

    assert ergebnis is not None and ergebnis.state == "completed"
    assert [tuple(b.rationale_codes) for b in _bewertungen(factory)] == [
        ("nicht_bearbeitet",)
    ] * 2
