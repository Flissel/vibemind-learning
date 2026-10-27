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


# --- 27.10.: the terminal course draft (job 8851ed7f) taught nothing in ten of fifteen
# lesson texts ("In diesem Abschnitt lernen Sie den Befehl kennen ..."); the facts sat
# only in claims, which the lesson page never shows. It also mixed "Sie" and "du" and
# spread a 1,074-word guide over fifteen chapters.

def _german_lesson(ids, body: str, claim: str):
    from spaces.learning.services.course_factory.schemas import Claim

    draft = _draft(ids)
    draft.lessons[0].body = body
    draft.lessons[0].claims = [Claim(claim_id="claim-authority", text=claim, citation_ids=[ids["citation"]])]
    return draft


_CLAIM = ("Um Claude Code in Windows PowerShell zu installieren, fügt man den Befehl "
          "'irm https://claude.ai/install.ps1 | iex' in PowerShell ein und führt ihn mit Enter aus.")


def test_a_lesson_text_that_only_announces_its_content_is_held() -> None:
    from spaces.learning.services.course_factory.quality_gate import _deterministic_quality_issues

    draft = _german_lesson(_ids(), "In diesem Abschnitt lernen Sie den Installationsbefehl kennen. "
                                   "Die anschließenden Übungen prüfen Ihr Wissen.", _CLAIM)

    codes = {issue.code for issue in _deterministic_quality_issues(draft)}

    assert "lesson_body_thin" in codes


def test_a_lesson_text_that_teaches_its_claims_passes() -> None:
    from spaces.learning.services.course_factory.quality_gate import _deterministic_quality_issues

    body = ("Um Claude Code in Windows PowerShell zu installieren, fügen Sie diesen Befehl ein "
            "und führen ihn mit Enter aus:\n\n```powershell\nirm https://claude.ai/install.ps1 | iex\n```")
    draft = _german_lesson(_ids(), body, _CLAIM)

    codes = {issue.code for issue in _deterministic_quality_issues(draft)}

    assert "lesson_body_thin" not in codes and "lesson_command_missing" not in codes


def test_a_command_named_in_the_claims_must_stand_verbatim_in_the_lesson_text() -> None:
    from spaces.learning.services.course_factory.quality_gate import _deterministic_quality_issues

    body = ("Um Claude Code in Windows PowerShell zu installieren, fügen Sie den Installationsbefehl "
            "in PowerShell ein und führen ihn mit Enter aus.")
    draft = _german_lesson(_ids(), body, _CLAIM)

    issues = [i for i in _deterministic_quality_issues(draft) if i.code == "lesson_command_missing"]

    assert [i.subject_id for i in issues] == ["lesson-authority"] and issues[0].blocking


def test_a_course_that_mixes_formal_and_informal_address_is_held() -> None:
    from spaces.learning.services.course_factory.quality_gate import _deterministic_quality_issues

    ids = _ids()
    draft = _german_lesson(ids, "Öffnen Sie PowerShell. " + _CLAIM, _CLAIM)
    draft.activities[0].prompt = "Welchen Befehl gibst du ein, und woran erkennst du den Erfolg?"

    codes = {issue.code for issue in _deterministic_quality_issues(draft)}

    assert "address_mixed" in codes


def test_formal_address_throughout_and_sentence_initial_sie_are_not_mixed() -> None:
    from spaces.learning.services.course_factory.quality_gate import _deterministic_quality_issues

    ids = _ids()
    draft = _german_lesson(ids, "Öffnen Sie PowerShell. Sie sehen dann die Eingabezeile. " + _CLAIM, _CLAIM)
    draft.activities[0].prompt = "Welchen Befehl geben Sie ein? Nennen Sie das Ergebnis."

    codes = {issue.code for issue in _deterministic_quality_issues(draft)}

    assert "address_mixed" not in codes


@pytest.mark.parametrize(("words", "chapters", "held"), [(1074, 8, False), (1074, 15, True), (200, 3, False), (200, 4, True)])
def test_the_chapter_count_stays_in_proportion_to_the_source(words, chapters, held) -> None:
    from spaces.learning.services.course_factory.quality_gate import _source_proportion_issues
    from spaces.learning.services.course_factory.schemas import ChapterDraft
    from spaces.learning.services.course_factory.verification import ChunkEvidence, SourceSnapshot

    ids = _ids()
    draft = _draft(ids)
    for index in range(chapters - 1):
        draft.chapters.append(ChapterDraft(chapter_id=f"extra-{index}", title=f"Kapitel {index}",
                                           lesson_ids=["lesson-authority"], activity_ids=["activity-authority"]))
    text = " ".join(["Wort"] * words)
    sources = (SourceSnapshot(source_id=ids["source"], revision=2, content_hash="a" * 64, chunks=(
        ChunkEvidence(chunk_id=ids["chunk"], content_hash="b" * 64, locator={"page": 4}, text=text),)),)

    codes = {issue.code for issue in _source_proportion_issues(draft, sources)}

    assert ("too_many_chapters" in codes) is held


def test_without_source_texts_the_chapter_count_is_not_judged() -> None:
    from spaces.learning.services.course_factory.quality_gate import _source_proportion_issues

    ids = _ids()

    assert _source_proportion_issues(_draft(ids), _catalog(ids)) == []


def test_a_command_with_escaped_inner_quotes_in_a_claim_is_found_in_the_lesson_text() -> None:
    # Live (27.10.): the PATH lesson had every command, but a claim wrote Test-Path \"...\"
    # with escaped inner quotes, and the quote pattern stopped at them.
    from spaces.learning.services.course_factory.quality_gate import _deterministic_quality_issues

    claim = 'Ob das Programm da ist, zeigt der Befehl „Test-Path \\"$env:USERPROFILE\.local\bin\claude.exe\\"“ in PowerShell an.'
    body = ('Ob das Programm da ist, zeigt dieser Befehl in PowerShell an:\n\n'
            '```powershell\nTest-Path "$env:USERPROFILE\.local\bin\claude.exe"\n```')
    draft = _german_lesson(_ids(), body, claim)

    codes = {issue.code for issue in _deterministic_quality_issues(draft)}

    assert "lesson_command_missing" not in codes
