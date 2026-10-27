from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.course_factory.quality_gate import QualityGate
from spaces.learning.services.course_factory.repository import (
    CourseFactoryRepository,
    SourceProvenance,
    StageArtifactInput,
)
from spaces.learning.services.course_factory.roles.schemas import QualityReviewOutput
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.db.models import Base
from spaces.learning.tests.unit.test_source_verification import _catalog, _draft, _ids


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture()
def session_factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'quality.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _quality_job(repository: CourseFactoryRepository, course_id: str):
    job = repository.create_job(
        course_id=course_id,
        request_hash=_hash("request"),
        provenance=(
            SourceProvenance(
                source_id=str(uuid4()), revision=1, content_hash=_hash("source")
            ),
        ),
    )
    for current, target in (
        (FactoryState.QUEUED, FactoryState.INGESTING),
        (FactoryState.INGESTING, FactoryState.STRUCTURING),
        (FactoryState.STRUCTURING, FactoryState.AUTHORING),
        (FactoryState.AUTHORING, FactoryState.ASSESSING),
        (FactoryState.ASSESSING, FactoryState.VERIFYING),
        (FactoryState.VERIFYING, FactoryState.QUALITY_GATE),
    ):
        artifact = None
        if current is not FactoryState.QUEUED:
            artifact = StageArtifactInput(
                stage=current,
                input_hash=_hash(current.value + "-input"),
                output_hash=_hash(current.value + "-output"),
                evidence_refs=("test://evidence",),
            )
        job = repository.advance(
            job.id,
            expected_revision=job.revision,
            target=target,
            artifact=artifact,
        )
    repository.record_stage_artifact(
        job.id,
        expected_revision=job.revision,
        artifact=StageArtifactInput(
            stage=FactoryState.QUALITY_GATE,
            input_hash=_hash("quality-input"),
            output_hash=_hash("quality-output"),
            evidence_refs=("openfang://completion/quality",),
        ),
    )
    return job


def _review(score: float = 0.9, decision: str = "pass") -> QualityReviewOutput:
    return QualityReviewOutput(
        schema_version="quality-review-v1",
        decision=decision,
        issues=[] if decision == "pass" else ["Needs revision"],
        score=score,
    )


def test_valid_grounded_course_is_promoted_to_review_ready(session_factory) -> None:
    ids = _ids()
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    gate = QualityGate(repository)
    draft = _draft(ids)
    draft.job_id = job.id

    result = gate.evaluate_and_promote(
        job.id,
        expected_revision=job.revision,
        draft=draft,
        sources=_catalog(ids),
        ai_review=_review(),
    )

    assert result.report.approved is True
    assert result.job.state is FactoryState.REVIEW_READY


def test_duplicate_content_and_missing_concept_coverage_remain_in_quality_gate(
    session_factory,
) -> None:
    ids = _ids()
    draft = _draft(ids)
    duplicate = draft.lessons[0].model_copy(deep=True)
    duplicate.lesson_id = "lesson-duplicate"
    draft.lessons.append(duplicate)
    draft.chapters[0].lesson_ids.append(duplicate.lesson_id)
    draft.concepts.append(
        draft.concepts[0].model_copy(
            update={"concept_id": "uncovered", "title": "Uncovered"}
        )
    )
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft.job_id = job.id

    result = QualityGate(repository).evaluate_and_promote(
        job.id,
        expected_revision=job.revision,
        draft=draft,
        sources=_catalog(ids),
        ai_review=_review(),
    )

    assert result.job.state is FactoryState.QUALITY_GATE
    assert "duplicate_lesson" in result.report.issue_codes
    assert "concept_missing_lesson" in result.report.issue_codes
    assert "concept_missing_activity" in result.report.issue_codes


def test_task_distribution_rubric_and_solvability_are_required(session_factory) -> None:
    ids = _ids()
    draft = _draft(ids)
    draft.activities[0].rubric = []
    draft.activities[0].expected_answer = ""
    for index in range(3):
        clone = draft.activities[0].model_copy(deep=True)
        clone.activity_id = f"case-{index}"
        draft.activities.append(clone)
        draft.chapters[0].activity_ids.append(clone.activity_id)
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft.job_id = job.id

    result = QualityGate(repository).evaluate_and_promote(
        job.id,
        expected_revision=job.revision,
        draft=draft,
        sources=_catalog(ids),
        ai_review=_review(),
    )

    assert result.report.approved is False
    assert "task_type_distribution" in result.report.issue_codes
    assert "rubric_missing" in result.report.issue_codes
    assert "expected_answer_missing" in result.report.issue_codes


@pytest.mark.parametrize(("activities", "held"), [(6, False), (7, True)])
def test_a_chapter_may_not_pile_many_tasks_on_few_lessons(activities, held) -> None:
    # Live (22.10.): a retry collapsed ten chapters into one "summary" chapter with a
    # single lesson and twelve tasks; both reviews passed it.
    from spaces.learning.services.course_factory.quality_gate import (
        _deterministic_quality_issues,
    )

    draft = _draft(_ids())
    for index in range(activities - len(draft.activities)):
        clone = draft.activities[0].model_copy(deep=True)
        clone.activity_id = f"extra-{index}"
        draft.activities.append(clone)
        draft.chapters[0].activity_ids.append(clone.activity_id)

    codes = {issue.code for issue in _deterministic_quality_issues(draft)}

    assert ("chapter_lesson_thin" in codes) is held


def _blocking_review() -> QualityReviewOutput:
    return QualityReviewOutput(
        schema_version="quality-review-v1",
        decision="revise",
        issues=["Kapiteltitel zu lang", "Antwort widerspricht der Quelle"],
        blocking_issues=["Antwort widerspricht der Quelle"],
        score=0.8,
    )


def test_minor_ai_findings_do_not_hold_a_course(session_factory) -> None:
    # Live (21.-23.10.): the AI reviewer sent back every attempt of four runs, often
    # for style or coverage remarks; only blocking findings hold a draft now.
    ids = _ids()
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft = _draft(ids)
    draft.job_id = job.id
    review = QualityReviewOutput(
        schema_version="quality-review-v1",
        decision="pass",
        issues=["Kapiteltitel zu lang", "Erste Aufgabe ist leicht"],
        score=0.6,
    )

    result = QualityGate(repository).evaluate_and_promote(
        job.id,
        expected_revision=job.revision,
        draft=draft,
        sources=_catalog(ids),
        ai_review=review,
    )

    assert result.job.state is FactoryState.REVIEW_READY


@pytest.mark.parametrize(
    "review",
    # A review in the old format (decision only) still holds when it says revise.
    [_blocking_review(), _review(decision="revise")],
)
def test_blocking_or_legacy_revise_ai_review_cannot_promote(
    session_factory, review: QualityReviewOutput
) -> None:
    ids = _ids()
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft = _draft(ids)
    draft.job_id = job.id

    result = QualityGate(repository).evaluate_and_promote(
        job.id,
        expected_revision=job.revision,
        draft=draft,
        sources=_catalog(ids),
        ai_review=review,
    )

    assert result.job.state is FactoryState.QUALITY_GATE
    assert "ai_review_not_approved" in result.report.issue_codes


def test_draft_must_belong_to_current_job_attempt(session_factory) -> None:
    ids = _ids()
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft = _draft(ids)

    result = QualityGate(repository).evaluate_and_promote(
        job.id,
        expected_revision=job.revision,
        draft=draft,
        sources=_catalog(ids),
        ai_review=_review(),
    )

    assert result.job.state is FactoryState.QUALITY_GATE
    assert "draft_job_mismatch" in result.report.issue_codes


def test_duplicate_entity_ids_fail_closed(session_factory) -> None:
    ids = _ids()
    draft = _draft(ids)
    draft.concepts.append(draft.concepts[0].model_copy(deep=True))
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft.job_id = job.id

    result = QualityGate(repository).evaluate_and_promote(
        job.id,
        expected_revision=job.revision,
        draft=draft,
        sources=_catalog(ids),
        ai_review=_review(),
    )

    assert result.job.state is FactoryState.QUALITY_GATE
    assert "duplicate_concept_id" in result.report.issue_codes


def test_deterministic_preflight_surfaces_declared_unsupported_claims(
    session_factory,
) -> None:
    ids = _ids()
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft = _draft(ids)
    draft.job_id = job.id

    report = QualityGate(repository).inspect(
        job.id,
        expected_revision=job.revision,
        draft=draft,
        sources=_catalog(ids),
        declared_unsupported_claim_ids=("claim-authority",),
    )

    assert report.approved is False
    assert report.issue_codes == ["unsupported_claim"]
    assert repository.get(job.id).state is FactoryState.QUALITY_GATE


def _quiz(options, correct):
    from spaces.learning.services.course_factory.schemas import ActivityDraft, QuizOptionDraft

    return ActivityDraft(
        activity_id="quiz-1",
        chapter_id="foundations",
        type="quiz",
        concept_ids=["authority"],
        prompt="Which path is authorized?",
        expected_answer="The OpenFang path.",
        citation_ids=[],
        rubric=[],
        options=[QuizOptionDraft(option_id=o, text=f"Option {o}") for o in options],
        correct_option_ids=correct,
    )


@pytest.mark.parametrize(
    ("options", "correct", "valid"),
    [
        (["a", "b", "c"], ["b"], True),
        (["a", "b", "c"], ["a", "c"], True),
        (["a"], ["a"], False),
        (["a", "b"], [], False),
        (["a", "b"], ["z"], False),
        (["a", "a"], ["a"], False),
    ],
)
def test_quiz_needs_structured_options_with_a_correct_answer(options, correct, valid) -> None:
    # Live, quiz options and the solution sat inside the prompt text; practice
    # could not score them without a model and the learner saw the answer.
    from spaces.learning.services.course_factory.quality_gate import _deterministic_quality_issues

    ids = _ids()
    draft = _draft(ids)
    draft.activities.append(_quiz(options, correct))
    draft.chapters[0].activity_ids.append("quiz-1")

    codes = {issue.code for issue in _deterministic_quality_issues(draft)}
    assert ("quiz_options_invalid" not in codes) is valid


def test_structured_quiz_needs_no_expected_answer_text() -> None:
    # Live: 15 structured quizzes left expected_answer empty (the solution is in
    # correct_option_ids) and the gate held the draft with expected_answer_missing.
    from spaces.learning.services.course_factory.quality_gate import _deterministic_quality_issues

    ids = _ids()
    draft = _draft(ids)
    quiz = _quiz(["a", "b", "c"], ["a"])
    quiz.expected_answer = ""
    draft.activities.append(quiz)
    draft.chapters[0].activity_ids.append("quiz-1")

    codes = {(issue.code, issue.subject_id) for issue in _deterministic_quality_issues(draft)}
    assert ("expected_answer_missing", "quiz-1") not in codes


def _guessable_quiz(ids: dict[str, str]):
    from spaces.learning.services.course_factory.schemas import ActivityDraft, QuizOptionDraft

    return ActivityDraft(
        activity_id="quiz-ratbar", chapter_id="foundations", type="quiz", concept_ids=["authority"],
        prompt="Was gilt?", expected_answer="", citation_ids=[ids["citation"]], rubric=[],
        options=[QuizOptionDraft(option_id=o, text=f"Option {o}") for o in "abcd"], correct_option_ids=["a", "b", "c"],
    )


def test_a_quiz_with_all_but_one_option_right_is_held(session_factory) -> None:
    ids = _ids()
    draft = _draft(ids)
    draft.activities.append(_guessable_quiz(ids))
    draft.chapters[0].activity_ids.append("quiz-ratbar")
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft.job_id = job.id

    result = QualityGate(repository).evaluate_and_promote(
        job.id, expected_revision=job.revision, draft=draft, sources=_catalog(ids), ai_review=_review(),
    )

    assert result.report.approved is False and "quiz_all_but_one" in result.report.issue_codes


def test_an_uneven_rubric_scale_is_noted_but_does_not_hold_the_course(session_factory) -> None:
    ids = _ids()
    draft = _draft(ids)
    big = draft.activities[0].model_copy(deep=True)
    big.activity_id = "case-hundert"
    big.rubric[0].points = 100
    draft.activities.append(big)
    draft.chapters[0].activity_ids.append("case-hundert")
    repository = CourseFactoryRepository(session_factory)
    job = _quality_job(repository, str(uuid4()))
    draft.job_id = job.id

    result = QualityGate(repository).evaluate_and_promote(
        job.id, expected_revision=job.revision, draft=draft, sources=_catalog(ids), ai_review=_review(),
    )

    assert "rubric_scale_inconsistent" in result.report.issue_codes
    assert result.report.approved is True and result.job.state is FactoryState.REVIEW_READY
