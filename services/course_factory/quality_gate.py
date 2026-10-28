from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from spaces.learning.services.course_factory.repository import (
    CourseFactoryRepository,
    FactoryJobRecord,
)
from spaces.learning.services.course_factory.roles.schemas import QualityReviewOutput
from spaces.learning.services.course_factory.schemas import ActivityDraft, CourseDraft
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.course_factory.verification import (
    SourceSnapshot,
    verify_course_sources,
)


_MAX_ACTIVITIES_PER_LESSON = 6
# 27.10. (terminal course, job 8851ed7f): ten of fifteen lesson texts only announced
# their content ("In diesem Abschnitt lernen Sie den Befehl kennen ..."); the facts sat
# in claims, which the lesson page does not show. Measured on both real drafts: lessons
# that teach cover 0.67 to 1.0 of their claims' words, announcing ones 0.09 to 0.43.
_MIN_BODY_COVERAGE = 0.6
_MIN_CLAIM_WORDS = 5
_CONTENT_WORD = re.compile(r"[^\W_][\w.-]{4,}")
_QUOTED = re.compile(r"[`'\"\u201e\u201c\u201a]([^`'\"\u201e\u201c\u201d\u201a\u2018\n]{2,200})[`'\"\u201c\u201d\u2018]")
_FENCED = re.compile(r"```.*?```", re.S)
_INFORMAL = re.compile(r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner|deines)\b")
_FORMAL = re.compile(r"\b(Sie|Ihnen|Ihr|Ihre|Ihren|Ihrem|Ihrer|Ihres)\b")
# A task with its options or solution written into the lesson text (job 50638c2a: "Aufgabe 1
# (Auswahlaufgabe) ... Richtig ist A"): a line that starts like one, not the word in prose.
_TASK_LINE = re.compile(r"(?mi)^\s*(?:\*\*)?(?:aufgabe\s+\d+\b|erwartete antwort\b|richtig ist\b|l(?:ö|oe)sung:)")
# One chapter per this many source words at most (the 1,074-word terminal guide had 15).
_WORDS_PER_CHAPTER = 150
_MIN_CHAPTERS = 3


def blocking_review_issues(review: QualityReviewOutput) -> list[str]:
    """The AI findings that hold a draft.

    Live (21.-23.10.) the reviewer sent back every attempt of four runs, often for
    style or coverage remarks. Only factual errors, unsupported statements, wrong
    expected answers and unsolvable tasks block now; the score is informational.
    A review in the older format (no blocking_issues) holds when it says revise.
    """
    if review.blocking_issues:
        return list(review.blocking_issues)
    if review.decision == "revise" and "blocking_issues" not in review.model_fields_set:
        return list(review.issues) or ["revise"]
    return []


@dataclass(frozen=True)
class QualityIssue:
    code: str
    subject_id: str
    # A note (blocking=False) is listed but does not hold the course.
    blocking: bool = True


@dataclass(frozen=True)
class QualityReport:
    issues: tuple[QualityIssue, ...]

    @property
    def approved(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    @property
    def issue_codes(self) -> list[str]:
        return list(dict.fromkeys(issue.code for issue in self.issues))


@dataclass(frozen=True)
class QualityGateResult:
    job: FactoryJobRecord
    report: QualityReport


class QualityGate:
    def __init__(self, repository: CourseFactoryRepository) -> None:
        self._repository = repository

    def evaluate_and_promote(
        self,
        job_id: str,
        *,
        expected_revision: int,
        draft: CourseDraft,
        sources: tuple[SourceSnapshot, ...],
        ai_review: QualityReviewOutput,
    ) -> QualityGateResult:
        report = self.inspect(
            job_id,
            expected_revision=expected_revision,
            draft=draft,
            sources=sources,
        )
        issues = list(report.issues)
        if blocking_review_issues(ai_review):
            issues.append(QualityIssue("ai_review_not_approved", draft.job_id))

        report = QualityReport(tuple(_deduplicate_issues(issues)))
        job = self._repository.get(job_id)
        if report.approved:
            job = self._repository.advance(
                job_id,
                expected_revision=expected_revision,
                target=FactoryState.REVIEW_READY,
            )
        return QualityGateResult(job=job, report=report)

    def inspect(
        self,
        job_id: str,
        *,
        expected_revision: int,
        draft: CourseDraft,
        sources: tuple[SourceSnapshot, ...],
        declared_unsupported_claim_ids: tuple[str, ...] = (),
    ) -> QualityReport:
        job = self._repository.get(job_id)
        if job.revision != expected_revision:
            raise ValueError("quality gate revision mismatch")
        if job.state is not FactoryState.QUALITY_GATE:
            raise ValueError("quality gate requires quality_gate state")

        issues = [
            QualityIssue(issue.code, issue.subject_id)
            for issue in verify_course_sources(draft, sources).issues
        ]
        if draft.job_id != job.id or draft.attempt_number != job.attempt_number:
            issues.append(QualityIssue("draft_job_mismatch", draft.job_id))
        for claim_id in declared_unsupported_claim_ids:
            issues.append(QualityIssue("unsupported_claim", claim_id))
        issues.extend(_deterministic_quality_issues(draft))
        issues.extend(_source_proportion_issues(draft, sources))
        return QualityReport(tuple(_deduplicate_issues(issues)))


def _deterministic_quality_issues(draft: CourseDraft) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for code, values in (
        ("duplicate_concept_id", [item.concept_id for item in draft.concepts]),
        ("duplicate_chapter_id", [item.chapter_id for item in draft.chapters]),
        ("duplicate_lesson_id", [item.lesson_id for item in draft.lessons]),
        ("duplicate_activity_id", [item.activity_id for item in draft.activities]),
        ("duplicate_citation_id", [item.citation_id for item in draft.citations]),
    ):
        for duplicate in _duplicates(values):
            issues.append(QualityIssue(code, duplicate))
    concept_ids = {concept.concept_id for concept in draft.concepts}
    chapter_ids = {chapter.chapter_id for chapter in draft.chapters}
    lesson_ids = {lesson.lesson_id for lesson in draft.lessons}
    activity_ids = {activity.activity_id for activity in draft.activities}

    for chapter in draft.chapters:
        # A retry once collapsed a whole course into one chapter with one lesson
        # and twelve tasks; both reviews passed it.
        if len(chapter.activity_ids) > _MAX_ACTIVITIES_PER_LESSON * max(1, len(chapter.lesson_ids)):
            issues.append(QualityIssue("chapter_lesson_thin", chapter.chapter_id))
        for lesson_id in chapter.lesson_ids:
            if lesson_id not in lesson_ids:
                issues.append(QualityIssue("chapter_lesson_missing", lesson_id))
        for activity_id in chapter.activity_ids:
            if activity_id not in activity_ids:
                issues.append(QualityIssue("chapter_activity_missing", activity_id))

    normalized_lessons: dict[str, str] = {}
    for lesson in draft.lessons:
        if lesson.chapter_id not in chapter_ids:
            issues.append(QualityIssue("lesson_chapter_missing", lesson.lesson_id))
        normalized = _normalize(lesson.title + " " + lesson.body)
        if normalized in normalized_lessons:
            issues.append(QualityIssue("duplicate_lesson", lesson.lesson_id))
        else:
            normalized_lessons[normalized] = lesson.lesson_id
        for concept_id in lesson.concept_ids:
            if concept_id not in concept_ids:
                issues.append(QualityIssue("lesson_concept_missing", concept_id))

    for activity in draft.activities:
        if activity.chapter_id not in chapter_ids:
            issues.append(QualityIssue("activity_chapter_missing", activity.activity_id))
        for concept_id in activity.concept_ids:
            if concept_id not in concept_ids:
                issues.append(QualityIssue("activity_concept_missing", concept_id))
        issues.extend(activity_issues(activity))

    scales = {sum(criterion.points for criterion in activity.rubric) for activity in draft.activities if activity.rubric}
    if len(scales) > 1:
        issues.append(QualityIssue("rubric_scale_inconsistent", draft.job_id, blocking=False))

    lesson_coverage = {
        concept_id for lesson in draft.lessons for concept_id in lesson.concept_ids
    }
    activity_coverage = {
        concept_id
        for activity in draft.activities
        for concept_id in activity.concept_ids
    }
    for concept_id in sorted(concept_ids):
        if concept_id not in lesson_coverage:
            issues.append(QualityIssue("concept_missing_lesson", concept_id))
        if concept_id not in activity_coverage:
            issues.append(QualityIssue("concept_missing_activity", concept_id))

    if len(draft.activities) >= 4:
        counts = Counter(activity.type for activity in draft.activities)
        if len(counts) < 2 or max(counts.values()) / len(draft.activities) > 0.75:
            issues.append(QualityIssue("task_type_distribution", draft.job_id))

    for lesson in draft.lessons:
        issues.extend(lesson_teaching_issues(lesson))
    if mixes_address([lesson.body for lesson in draft.lessons] + [activity.prompt for activity in draft.activities]):
        issues.append(QualityIssue("address_mixed", draft.job_id))
    return issues


def activity_issues(activity: ActivityDraft) -> list[QualityIssue]:
    """The rules one activity answers to on its own (no course around it)."""
    issues: list[QualityIssue] = []
    # A structured quiz carries its solution in correct_option_ids.
    structured_quiz = activity.type == "quiz" and _quiz_options_valid(activity)
    if not activity.expected_answer.strip() and not structured_quiz:
        issues.append(QualityIssue("expected_answer_missing", activity.activity_id))
    if activity.type != "quiz" and not activity.rubric:
        issues.append(QualityIssue("rubric_missing", activity.activity_id))
    if activity.type == "quiz" and not _quiz_options_valid(activity):
        issues.append(QualityIssue("quiz_options_invalid", activity.activity_id))
    # "All but one" can be solved by spotting the odd option, not by knowing the matter.
    if structured_quiz and len(activity.options) >= 3 and len(activity.correct_option_ids) == len(activity.options) - 1:
        issues.append(QualityIssue("quiz_all_but_one", activity.activity_id))
    return issues


def lesson_teaching_issues(lesson) -> list[QualityIssue]:
    """The lesson text is what the learner reads: it has to carry the claims, and every
    command a claim names word for word."""
    from spaces.learning.services.lernsequenz.aus_struktur import KNOWN_COMMANDS

    issues: list[QualityIssue] = []
    if _TASK_LINE.search(lesson.body):
        issues.append(QualityIssue("lesson_contains_tasks", lesson.lesson_id))
    claims = " ".join(claim.text for claim in lesson.claims)
    words = {word.lower() for word in _CONTENT_WORD.findall(claims)}
    body = lesson.body.lower()
    if len(words) >= _MIN_CLAIM_WORDS and sum(word in body for word in words) / len(words) < _MIN_BODY_COVERAGE:
        issues.append(QualityIssue("lesson_body_thin", lesson.lesson_id))
    known = tuple(command.strip() for command in KNOWN_COMMANDS)
    squashed_body = " ".join(lesson.body.split())
    for quoted in _QUOTED.findall(claims):
        # An escaped inner quote (Test-Path \"...\") ends the match early; what stands before it counts.
        command = " ".join(quoted.split()).rstrip("\\ ")
        first = command.split(" ", 1)[0] if command else ""
        is_command = first in known or any(command.startswith(k) for k in known if k[:1] in "$[")
        if is_command and command not in squashed_body:
            issues.append(QualityIssue("lesson_command_missing", lesson.lesson_id))
            break
    return issues


def mixes_address(texts: list[str]) -> bool:
    """Formal "Sie" and informal "du" in the same course. Commands, code and quotes are left
    out (CMD's `dir` is not "dir"); a sentence-initial "Sie" may mean "they" and does not count."""
    from spaces.learning.services.lernsequenz.aus_struktur import command_lines

    informal = formal = False
    for text in texts:
        prose = _QUOTED.sub(" ", _FENCED.sub(" ", text))
        commands = set(command_lines(text))
        prose = "\n".join(line for line in prose.splitlines() if line.strip() not in commands)
        informal = informal or bool(_INFORMAL.search(prose))
        for match in _FORMAL.finditer(prose):
            before = prose[: match.start()].rstrip()
            if before and before[-1] not in ".!?:\n":
                formal = True
                break
    return informal and formal


_lesson_teaching_issues = lesson_teaching_issues
_mixes_address = mixes_address


def _source_proportion_issues(draft: CourseDraft, sources: tuple[SourceSnapshot, ...]) -> list[QualityIssue]:
    """A short guide split into many one-lesson chapters reads like a table of contents.
    Judged only when the caller supplied chunk texts."""
    words = sum(len(chunk.text.split()) for source in sources for chunk in source.chunks if chunk.text)
    if not words:
        return []
    if len(draft.chapters) > max(_MIN_CHAPTERS, math.ceil(words / _WORDS_PER_CHAPTER)):
        return [QualityIssue("too_many_chapters", draft.job_id)]
    return []


def _quiz_options_valid(activity: ActivityDraft) -> bool:
    option_ids = [option.option_id for option in activity.options]
    correct = activity.correct_option_ids
    return (
        len(option_ids) >= 2
        and len(set(option_ids)) == len(option_ids)
        and bool(correct)
        and len(set(correct)) == len(correct)
        and set(correct) <= set(option_ids)
    )


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _duplicates(values: list[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _deduplicate_issues(issues: list[QualityIssue]) -> list[QualityIssue]:
    seen: set[tuple[str, str]] = set()
    result: list[QualityIssue] = []
    for issue in issues:
        key = (issue.code, issue.subject_id)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
