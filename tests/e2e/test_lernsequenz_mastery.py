"""A choice answered in the sequence player counts for the learner's mastery (engine-scored)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ConceptMastery,
    ItemConcept,
    LearningAttempt,
    LearningSession,
    MasteryEvidence,
)
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.db.models import Base


def _store(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'seq-mastery.db'}")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record) -> None:
        del record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    course, concept, item = str(uuid4()), str(uuid4()), str(uuid4())
    with factory() as db, db.begin():
        db.add(AdaptiveConcept(id=concept, course_id=course, course_revision=2, concept_key="pruefen", title="Prüfen"))
        db.add(AdaptiveItem(
            id=item, course_id=course, course_revision=2, activity_id="a1", item_type="single_choice",
            difficulty=0.5, quality_status="approved", expected_answer={"choice_id": "y"},
            scoring_config={"prompt": "Wann prüfen?", "options": [{"id": "x", "label": "Jährlich"},
                            {"id": "y", "label": "Vor jedem Einsatz"}], "source_refs": ["source://s/1"], "hints": []},
            rubric=[],
        ))
        db.add(ItemConcept(item_id=item, concept_id=concept, course_id=course, course_revision=2))
    return engine, factory, course


def test_right_and_wrong_answers_update_mastery_in_one_session(tmp_path: Path) -> None:
    engine, factory, course = _store(tmp_path)
    service = AdaptiveSessionService(factory)

    right = service.record_sequence_answer(actor_id="learning-claw", course_id=course, activity_id="a1", selected=["y"])
    wrong = service.record_sequence_answer(actor_id="learning-claw", course_id=course, activity_id="a1", selected=["x"])

    assert right["score"] == 1.0 and right["mastery_applied"] is True
    assert wrong["score"] == 0.0
    with factory() as db:
        sessions = db.scalars(select(LearningSession)).all()
        assert len(sessions) == 1 and sessions[0].blueprint["source"] == "lernsequenz"
        assert sorted(a.ordinal for a in db.scalars(select(LearningAttempt))) == [1, 2]
        assert db.scalar(select(func.count()).select_from(MasteryEvidence)) == 2
        assert db.scalar(select(ConceptMastery.actor_id)) == "learning-claw"
    assert service.record_sequence_answer(actor_id="learning-claw", course_id=course, activity_id="nope", selected=["y"]) is None
    engine.dispose()
