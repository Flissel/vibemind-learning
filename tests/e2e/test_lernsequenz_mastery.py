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


# --- over HTTP, for the signed-in person -------------------------------------------

def test_player_answers_count_for_the_person_in_the_token(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from spaces.learning.contracts.lernsequenz import LernsequenzV1
    from spaces.learning.deployment import lernsequenz_routes, runtime_api
    from spaces.learning.services.lernsequenz.person import person_token
    from spaces.learning.services.lernsequenz.store import SequenceStore
    from spaces.learning.services.lernsequenz.token import sequence_token
    from spaces.learning.tests.unit.test_lernsequenz import sequence_data

    engine, factory, course = _store(tmp_path)
    engine.dispose()
    key, person_key = "k" * 32, "shared-with-learnhouse-" + "x" * 16
    monkeypatch.setenv("LEARNING_DATABASE_URL", f"sqlite:///{tmp_path / 'seq-mastery.db'}")
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", key)
    monkeypatch.setenv("LEARNHOUSE_LEARNING_SERVICE_KEY", person_key)
    monkeypatch.delenv("LEARNING_SEQUENCE_ACTOR", raising=False)
    lernsequenz_routes._store.cache_clear()
    lernsequenz_routes._sessions.cache_clear()
    data = sequence_data()
    data["course_id"] = course
    data["checkpoints"][0]["activity_id"] = "a1"
    SequenceStore(tmp_path).save(LernsequenzV1.model_validate(data))
    client = TestClient(runtime_api.app, raise_server_exceptions=False)
    url = f"/lernsequenz/seq-roi/antwort?k={sequence_token('seq-roi', key)}"
    body = {"checkpoint_id": "q1", "answer": {"selected": ["b"]}}

    as_anna = client.post(url + "&p=" + person_token("lh-3-anna", key=person_key), json=body)
    anonymous = client.post(url, json=body)
    expired = client.post(url + "&p=" + person_token("lh-3-anna", key=person_key, now=1_000.0), json=body)
    lernsequenz_routes._store.cache_clear()
    lernsequenz_routes._sessions.cache_clear()

    assert as_anna.status_code == 200 and as_anna.json()["mastery"]["counted"] is True
    assert anonymous.status_code == 200 and anonymous.json()["mastery"]["counted"] is True
    assert expired.status_code == 401 and expired.json()["code"] == "person_expired"
    with factory() as db:
        assert sorted(db.scalars(select(ConceptMastery.actor_id))) == ["learning-claw", "lh-3-anna"]
