from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ItemConcept,
)
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    SessionStartCommand,
)
from spaces.learning.services.course_factory.practice_catalog import (
    PracticeCatalogProjector,
)
from spaces.learning.services.course_factory.schemas import ActivityDraft
from spaces.learning.services.db.models import Base
from spaces.learning.tests.unit.test_source_verification import _draft, _ids


class _NoEvaluator:
    def evaluate(self, request):  # pragma: no cover - never called here
        raise AssertionError("not evaluated in this test")


@pytest.fixture()
def factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'practice.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, connection_record) -> None:
        del connection_record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _quiz_draft(ids: dict[str, str]):
    draft = _draft(ids)
    quiz = ActivityDraft(
        activity_id="activity-quiz",
        chapter_id="foundations",
        type="quiz",
        concept_ids=["authority"],
        prompt="Which path is authorized?",
        expected_answer="The OpenFang path.",
        citation_ids=[ids["citation"]],
        rubric=[],
    )
    return draft.model_copy(update={"activities": [*draft.activities, quiz]})


def test_published_draft_becomes_a_practice_catalog_the_engine_can_serve(factory) -> None:
    # Live, practice failed for every factory course: publishing wrote no
    # adaptive concepts or items ("adaptive course revision has no concepts").
    ids = _ids()
    course_id = str(uuid4())
    draft = _quiz_draft(ids)

    written = PracticeCatalogProjector(factory).project(
        course_id=course_id, course_revision=5, draft=draft
    )

    assert written == 2
    with factory() as session:
        concepts = session.scalars(select(AdaptiveConcept)).all()
        items = {item.activity_id: item for item in session.scalars(select(AdaptiveItem))}
        links = session.scalar(select(func.count()).select_from(ItemConcept))
    assert [(c.concept_key, c.title, c.course_revision) for c in concepts] == [
        ("authority", "Authority", 5)
    ]
    assert links == 2
    case = items["activity-authority"]
    assert case.item_type == "case" and case.quality_status == "approved"
    assert case.expected_answer == {"text": "Route the request through OpenFang."}
    assert case.scoring_config["prompt"] == "Choose the authorized execution path."
    assert case.rubric == [
        {"criterion_id": "routing", "description": "Uses the authority chain", "points": 2}
    ]
    # Quiz has no structured options yet: it is graded by rubric against the answer.
    quiz = items["activity-quiz"]
    assert quiz.item_type == "open"
    assert len(quiz.rubric) == 1 and quiz.rubric[0]["points"] == 1

    service = AdaptiveSessionService(factory, rubric_evaluator=_NoEvaluator())
    turn = service.start(
        SessionStartCommand(
            course_id=course_id,
            course_revision=5,
            actor_id="local-owner",
            mode="training",
            blueprint={"max_attempts": 3},
        )
    )
    assert turn.task is not None
    assert turn.task.prompt in {
        "Choose the authorized execution path.",
        "Which path is authorized?",
    }


def test_projection_is_idempotent_per_course_revision(factory) -> None:
    ids = _ids()
    course_id = str(uuid4())
    projector = PracticeCatalogProjector(factory)

    assert projector.project(course_id=course_id, course_revision=5, draft=_draft(ids)) == 1
    assert projector.project(course_id=course_id, course_revision=5, draft=_draft(ids)) == 0

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AdaptiveItem)) == 1


def test_items_carry_a_section_hint_the_engine_serves(factory, monkeypatch) -> None:
    # Live, every hint request failed: "no approved hint is available".
    import json

    from spaces.learning.services.course_factory import practice_catalog

    ids = _ids()
    ref = json.dumps({"chunk_id": ids["chunk"], "locator": {"heading": "Vor der Arbeit"}})
    monkeypatch.setattr(
        practice_catalog, "_source_refs", lambda session, draft: {ids["citation"]: ref}
    )
    course_id = str(uuid4())
    PracticeCatalogProjector(factory).project(
        course_id=course_id, course_revision=5, draft=_draft(ids)
    )

    service = AdaptiveSessionService(factory, rubric_evaluator=_NoEvaluator())
    turn = service.start(
        SessionStartCommand(
            course_id=course_id,
            course_revision=5,
            actor_id="local-owner",
            mode="training",
            blueprint={"max_attempts": 1},
        )
    )
    hint = service.hint(turn.session_id, actor_id="local-owner")
    assert hint == "Lies noch einmal den Abschnitt „Vor der Arbeit“ in der Unterlage."


@pytest.mark.parametrize(
    ("correct", "item_type", "expected"),
    [(["b"], "single_choice", {"choice_id": "b"}), (["a", "c"], "multiple_choice", {"choice_ids": ["a", "c"]})],
)
def test_structured_quiz_becomes_a_choice_item_scored_without_a_model(
    factory, correct, item_type, expected
) -> None:
    from spaces.learning.services.course_factory.schemas import QuizOptionDraft

    ids = _ids()
    draft = _draft(ids)
    quiz = draft.activities[0].model_copy(
        update={
            "activity_id": "quiz-structured",
            "type": "quiz",
            "rubric": [],
            "options": [QuizOptionDraft(option_id=o, text=f"Option {o}") for o in ("a", "b", "c")],
            "correct_option_ids": correct,
        }
    )
    draft = draft.model_copy(update={"activities": [quiz]})
    course_id = str(uuid4())
    PracticeCatalogProjector(factory).project(course_id=course_id, course_revision=5, draft=draft)

    with factory() as session:
        item = session.scalar(select(AdaptiveItem))
    assert item.item_type == item_type
    assert item.expected_answer == expected
    # Shuffled per item; the content is unchanged.
    assert sorted(item.scoring_config["options"], key=lambda o: o["id"]) == [
        {"id": o, "label": f"Option {o}"} for o in ("a", "b", "c")
    ]

    # No rubric evaluator: the engine scores choice items deterministically.
    service = AdaptiveSessionService(factory)
    turn = service.start(
        SessionStartCommand(
            course_id=course_id,
            course_revision=5,
            actor_id="local-owner",
            mode="training",
            blueprint={"max_attempts": 1},
        )
    )
    assert turn.task is not None and sorted(o.id for o in turn.task.options) == ["a", "b", "c"]


def test_quiz_options_are_shuffled_so_the_answer_is_not_always_first(factory) -> None:
    # Live, every one of 15 quizzes had option "a" correct.
    from spaces.learning.services.course_factory.schemas import QuizOptionDraft

    ids = _ids()
    base = _draft(ids)
    quizzes = [
        base.activities[0].model_copy(
            update={
                "activity_id": f"quiz-{n}",
                "type": "quiz",
                "rubric": [],
                "options": [QuizOptionDraft(option_id=o, text=f"Option {o}") for o in "abcd"],
                "correct_option_ids": ["a"],
            }
        )
        for n in range(12)
    ]
    PracticeCatalogProjector(factory).project(
        course_id=str(uuid4()), course_revision=5, draft=base.model_copy(update={"activities": quizzes})
    )
    with factory() as session:
        items = list(session.scalars(select(AdaptiveItem)))
    firsts = {item.scoring_config["options"][0]["id"] for item in items}
    assert len(firsts) > 1
    for item in items:
        assert sorted(o["id"] for o in item.scoring_config["options"]) == list("abcd")
        assert item.expected_answer == {"choice_id": "a"}


def test_a_task_solved_correctly_is_not_asked_again_while_others_remain(factory) -> None:
    # Live (24.10.): a training session asked the same open task twice although the
    # first answer was fully correct.
    from spaces.learning.services.adaptive_engine.session_service import AnswerCommand
    from spaces.learning.services.course_factory.schemas import QuizOptionDraft

    ids = _ids()
    draft = _draft(ids)
    quizzes = [
        draft.activities[0].model_copy(
            update={
                "activity_id": f"quiz-{n}",
                "type": "quiz",
                "rubric": [],
                "options": [QuizOptionDraft(option_id=o, text=f"Option {o}") for o in ("a", "b")],
                "correct_option_ids": ["a"],
            }
        )
        for n in (1, 2)
    ]
    course_id = str(uuid4())
    PracticeCatalogProjector(factory).project(
        course_id=course_id, course_revision=5, draft=draft.model_copy(update={"activities": quizzes})
    )
    service = AdaptiveSessionService(factory)
    turn = service.start(
        SessionStartCommand(course_id=course_id, course_revision=5, actor_id="local-owner",
                            mode="training", blueprint={"max_attempts": 4})
    )
    solved = turn.task.id

    def answer(turn, choice):
        return service.answer(AnswerCommand(
            session_id=turn.session_id, actor_id="local-owner",
            expected_session_revision=turn.session_revision, answer={"choice_id": choice},
        ))

    turn = answer(turn, "a")  # correct
    assert turn.task.id != solved
    turn = answer(turn, "b")  # wrong: the unsolved task may come again, the solved one not
    assert turn.task.id != solved
    turn = answer(turn, "a")  # now both are solved: repeating is the only option left
    assert turn.task is not None


def test_training_can_be_limited_to_task_types(factory) -> None:
    # "Ich will die Zeichenaufgabe ueben": training picked any task type, so a
    # drawing task could come after a dozen others. item_types limits the pool.
    from spaces.learning.services.course_factory.schemas import QuizOptionDraft

    ids = _ids()
    draft = _draft(ids)
    quiz = draft.activities[0].model_copy(
        update={
            "activity_id": "quiz-1",
            "type": "quiz",
            "rubric": [],
            "options": [QuizOptionDraft(option_id=o, text=f"Option {o}") for o in ("a", "b")],
            "correct_option_ids": ["a"],
        }
    )
    course_id = str(uuid4())
    PracticeCatalogProjector(factory).project(
        course_id=course_id, course_revision=5,
        draft=draft.model_copy(update={"activities": [*draft.activities, quiz]}),
    )
    service = AdaptiveSessionService(factory, rubric_evaluator=_NoEvaluator())

    turn = service.start(
        SessionStartCommand(course_id=course_id, course_revision=5, actor_id="local-owner",
                            mode="training", blueprint={"max_attempts": 2, "item_types": ["case"]})
    )

    assert turn.task is not None and turn.task.representation == "case"
    with pytest.raises(LookupError):
        service.start(
            SessionStartCommand(course_id=course_id, course_revision=5, actor_id="local-owner",
                                mode="training",
                                blueprint={"max_attempts": 2, "item_types": ["penecho_canvas"]})
        )
