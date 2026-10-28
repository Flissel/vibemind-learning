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

    result = service.resolve_review(review_id, reviewer_id="reviewer-1", decision="confirm")

    assert result["score"] == 0.8 and result["decision"] == "confirm"
    assert service.pending_reviews() == []
    with factory() as db:
        manual = db.scalar(select(LearningEvaluation).where(LearningEvaluation.evaluator_type == "manual"))
        assert manual.accepted is True and manual.score == 0.8 and manual.confidence == 1.0
        assert db.scalar(select(func.count()).select_from(MasteryEvidence)) == 1
        assert db.scalar(select(LearningReviewItem.status)) == "resolved"
        decision = db.scalar(select(LearningReviewDecision))
        assert decision.reviewer_id == "reviewer-1" and decision.human_evaluation_id == manual.id
    with pytest.raises(PersistenceConflict):
        service.resolve_review(review_id, reviewer_id="reviewer-1", decision="confirm")


def test_overriding_uses_the_persons_score_everywhere(held) -> None:
    service, _, session_id = held
    review_id = service.pending_reviews()[0]["review_id"]

    result = service.resolve_review(
        review_id, reviewer_id="reviewer-1", decision="override", score=0.2, note="Zwinge fehlt"
    )

    assert result["score"] == 0.2
    summary = service.current(session_id, actor_id="learner-1").summary
    assert summary.results[0].score == 0.2 and summary.attempted == 1


def test_an_override_needs_a_score_between_0_and_1(held) -> None:
    service, _, _ = held
    review_id = service.pending_reviews()[0]["review_id"]
    for bad in (None, 1.5, -0.1):
        with pytest.raises(ValueError):
            service.resolve_review(review_id, reviewer_id="reviewer-1", decision="override", score=bad)
    with pytest.raises(ValueError):
        service.resolve_review(review_id, reviewer_id="reviewer-1", decision="maybe")


@pytest.fixture()
def api(held, tmp_path: Path, monkeypatch):
    from spaces.learning.deployment import runtime_api
    from spaces.learning.deployment.review_routes import ReviewRuntime, get_review_runtime
    from spaces.learning.tests.integration.course_factory_api_support import (
        SERVICE_KEY, SERVICE_KEY_ENV,
    )
    from fastapi.testclient import TestClient

    service, factory, session_id = held
    monkeypatch.setenv(SERVICE_KEY_ENV, SERVICE_KEY)
    (tmp_path / "artifacts").mkdir()
    runtime = ReviewRuntime(service=service, artifact_root=tmp_path / "artifacts")
    runtime_api.app.dependency_overrides[get_review_runtime] = lambda: runtime
    yield TestClient(runtime_api.app, raise_server_exceptions=False), factory, tmp_path / "artifacts"
    runtime_api.app.dependency_overrides.clear()


def test_the_api_lists_and_decides_reviews_for_reviewers_only(api) -> None:
    from spaces.learning.tests.integration.course_factory_api_support import headers

    client, _, _ = api
    assert client.get("/api/v1/reviews", headers=headers("learning.author")).status_code == 403
    listed = client.get("/api/v1/reviews", headers=headers("learning.reviewer")).json()["reviews"]
    review_id = listed[0]["review_id"]
    detail = client.get(f"/api/v1/reviews/{review_id}", headers=headers("learning.reviewer")).json()
    assert detail["answer_text"] and detail["drawing_png_base64"] is None

    url = f"/api/v1/reviews/{review_id}/decision"
    bad = client.post(url, json={"decision": "override", "score": 2}, headers=headers("learning.reviewer"))
    assert bad.status_code == 400
    decided = client.post(url, json={"decision": "override", "score": 0.3, "note": "knapp"},
                          headers=headers("learning.reviewer"))
    assert decided.status_code == 200 and decided.json()["score"] == 0.3
    again = client.post(url, json={"decision": "confirm"}, headers=headers("learning.reviewer"))
    assert again.status_code == 409
    assert client.get(f"/api/v1/reviews/{review_id}", headers=headers("learning.reviewer")).status_code == 404


def test_the_drawing_is_read_only_from_inside_the_artifact_root(api, tmp_path: Path) -> None:
    from spaces.learning.deployment.review_routes import ReviewRuntime, _drawing

    root = tmp_path / "artifacts"
    (root / "canvas" / "s").mkdir(parents=True)
    (root / "canvas" / "s" / "a.png").write_bytes(b"\x89PNG-drawing")
    (tmp_path / "outside.png").write_bytes(b"secret")
    runtime = ReviewRuntime(service=None, artifact_root=root)

    assert _drawing(runtime, "artifact://learning/canvas/s/a.png") == b"\x89PNG-drawing"
    assert _drawing(runtime, "artifact://learning/../outside.png") is None
    assert _drawing(runtime, "file:///etc/passwd") is None
