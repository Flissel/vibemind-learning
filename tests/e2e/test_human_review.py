"""Human review: a person confirms or overrides an AI evaluation held for review.

Live (24.10.) a drawing got 90 % at confidence 0.55; the decision went to the review
queue, but nothing could decide it, so the learner's mastery was never credited.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from spaces.learning.services.adaptive_engine.models import (
    LearningEvaluation,
    LearningReviewDecision,
    LearningReviewItem,
    MasteryEvidence,
)
from spaces.learning.services.adaptive_engine.repository import SessionInput
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    AnswerCommand,
)
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.tests.e2e.test_adaptive_session import _open_item_store, _RubricStub


@pytest.fixture()
def held(tmp_path: Path):
    engine, factory, course_id = _open_item_store(tmp_path)
    service = AdaptiveSessionService(factory, rubric_evaluator=_RubricStub(accepted=False))
    turn = service.start(SessionInput(course_id=course_id, course_revision=1, actor_id="learner-1",
                                      mode="training", blueprint={"max_attempts": 1}))
    service.answer(AnswerCommand(
        session_id=turn.session_id, actor_id="learner-1",
        expected_session_revision=turn.session_revision,
        answer={"text": "Ueber OpenFang, weil es autorisiert."},
    ))
    yield service, factory, turn.session_id
    engine.dispose()


def test_a_held_evaluation_is_listed_with_task_answer_and_ai_result(held) -> None:
    service, _, _ = held

    pending = service.pending_reviews()

    assert len(pending) == 1
    entry = pending[0]
    assert entry["reason"] == "low_confidence"
    assert entry["task_prompt"] == "Begruende den autorisierten Ausfuehrungspfad."
    assert entry["answer_text"] == "Ueber OpenFang, weil es autorisiert."
    assert entry["ai_score"] == 0.8 and entry["ai_confidence"] == 0.4
    assert entry["drawing_locator"] is None


def test_confirming_credits_mastery_and_resolves_the_review(held) -> None:
    service, factory, session_id = held
    review_id = service.pending_reviews()[0]["review_id"]

    result = service.resolve_review(review_id, reviewer_id="felix", decision="confirm")

    assert result["score"] == 0.8 and result["decision"] == "confirm"
    assert service.pending_reviews() == []
    with factory() as db:
        manual = db.scalar(select(LearningEvaluation).where(LearningEvaluation.evaluator_type == "manual"))
        assert manual.accepted is True and manual.score == 0.8 and manual.confidence == 1.0
        assert db.scalar(select(func.count()).select_from(MasteryEvidence)) == 1
        assert db.scalar(select(LearningReviewItem.status)) == "resolved"
        decision = db.scalar(select(LearningReviewDecision))
        assert decision.reviewer_id == "felix" and decision.human_evaluation_id == manual.id
    with pytest.raises(PersistenceConflict):
        service.resolve_review(review_id, reviewer_id="felix", decision="confirm")


def test_overriding_uses_the_persons_score_everywhere(held) -> None:
    service, _, session_id = held
    review_id = service.pending_reviews()[0]["review_id"]

    result = service.resolve_review(
        review_id, reviewer_id="felix", decision="override", score=0.2, note="Zwinge fehlt"
    )

    assert result["score"] == 0.2
    summary = service.current(session_id, actor_id="learner-1").summary
    assert summary.results[0].score == 0.2 and summary.attempted == 1


def test_an_override_needs_a_score_between_0_and_1(held) -> None:
    service, _, _ = held
    review_id = service.pending_reviews()[0]["review_id"]
    for bad in (None, 1.5, -0.1):
        with pytest.raises(ValueError):
            service.resolve_review(review_id, reviewer_id="felix", decision="override", score=bad)
    with pytest.raises(ValueError):
        service.resolve_review(review_id, reviewer_id="felix", decision="maybe")
