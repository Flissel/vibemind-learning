from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.adaptive_engine.models import (
    LearningAttempt,
    LearningEvaluation,
    LearningResponse,
)
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.db.models import Base


def _score(db, session_id: str, item_id: str, score: float, ordinal: int) -> None:
    attempt_id, response_id = str(uuid4()), str(uuid4())
    db.add(
        LearningAttempt(
            id=attempt_id, session_id=session_id, item_id=item_id, course_id="c",
            course_revision=1, ordinal=ordinal, selection_reason={},
        )
    )
    db.add(
        LearningResponse(
            id=response_id, attempt_id=attempt_id, response_revision=1,
            answer={}, answer_hash="h",
        )
    )
    db.add(
        LearningEvaluation(
            id=str(uuid4()), response_id=response_id, evaluator_type="deterministic", score=score,
            confidence=1.0, accepted=True, rationale_codes=[], evaluator_versions={},
            source_refs=[],
        )
    )


@pytest.mark.parametrize(
    ("score", "solved"), [(0.97, True), (0.9, True), (0.89, False), (0.5, False)]
)
def test_item_counts_as_solved_from_ninety_percent(score: float, solved: bool) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        _score(db, "s1", "item-a", score, 1)
        db.flush()
        result = AdaptiveSessionService._solved_item_ids(db, "s1")
    assert (("item-a" in result) is solved)
    engine.dispose()
