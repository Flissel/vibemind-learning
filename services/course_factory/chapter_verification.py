"""Chapter-wise source verification.

One verifier call that rewrote the whole draft ran past the subscription's 540 s
limit once a course had more than about a dozen tasks. The team's outputs are split
by the architect's chapters, each chapter is verified on its own, and the fragments
are merged here into one course draft without a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from spaces.learning.services.course_factory.roles.schemas import (
    ChapterVerificationOutput,
    SourceVerificationOutput,
)
from spaces.learning.services.course_factory.schemas import (
    CourseDraft,
    SourceProvenanceRef,
)


@dataclass
class ChapterSlice:
    index: int
    title: str
    concepts: list[str]
    lessons: list[dict[str, Any]] = field(default_factory=list)
    activities: list[dict[str, Any]] = field(default_factory=list)


def _norm(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def split_by_chapter(role_outputs: dict[str, Any]) -> list[ChapterSlice]:
    """Assign lessons and activities of the team to the architect's chapters."""
    titles = [str(t) for t in role_outputs["architect"]["chapters"]]
    coverage = {
        _norm(k): [str(c) for c in v]
        for k, v in role_outputs["concept_mapper"].get("chapter_coverage", {}).items()
    }
    slices = [
        ChapterSlice(index=i, title=t, concepts=coverage.get(_norm(t), []))
        for i, t in enumerate(titles, start=1)
    ]
    by_title = {_norm(s.title): s for s in slices}
    for lesson in role_outputs["lesson_author"]["lessons"]:
        target = by_title.get(_norm(lesson.get("chapter", ""))) or slices[-1]
        target.lessons.append(lesson)
    with_lessons = [s for s in slices if s.lessons] or slices
    for activity in role_outputs["assessment_designer"]["activities"]:
        concept = _norm(activity.get("concept", ""))
        target = next(
            (s for s in with_lessons if concept in {_norm(c) for c in s.concepts}),
            None,
        )
        if target is None:
            # Unmapped concept: the chapter with the fewest tasks so far.
            target = min(with_lessons, key=lambda s: (len(s.activities), s.index))
        target.activities.append(activity)
    return [s for s in with_lessons if s.lessons]


def course_title(target_outcome: str, *, fallback: str) -> str:
    for line in target_outcome.splitlines():
        if line.startswith("Course:"):
            title = line.removeprefix("Course:").strip()
            if title:
                return title[:500]
    return fallback[:500]


def merge_chapters(
    fragments: list[ChapterVerificationOutput],
    *,
    job_id: str,
    attempt_number: int,
    title: str,
    outcomes: list[str],
    source_refs: tuple[str, ...],
) -> SourceVerificationOutput:
    """Merge verified chapters into one draft; ids get a chapter prefix (k1-, k2-, ...)."""
    if not fragments:
        raise ValueError("no verified chapter to merge")
    chapters, lessons, activities, unsupported = [], [], [], []
    concepts: dict[str, Any] = {}
    citations: dict[str, Any] = {}
    for number, fragment in enumerate(fragments, start=1):
        prefix = f"k{number}-"
        lesson_ids = {lesson.lesson_id for lesson in fragment.lessons}
        activity_ids = {activity.activity_id for activity in fragment.activities}
        chapter = fragment.chapter
        if set(chapter.lesson_ids) - lesson_ids or set(chapter.activity_ids) - activity_ids:
            raise ValueError("verified chapter references lessons or tasks it does not contain")
        chapter_id = prefix + chapter.chapter_id
        chapters.append(
            chapter.model_copy(
                update={
                    "chapter_id": chapter_id,
                    "lesson_ids": [prefix + i for i in chapter.lesson_ids],
                    "activity_ids": [prefix + i for i in chapter.activity_ids],
                }
            )
        )
        for lesson in fragment.lessons:
            lessons.append(
                lesson.model_copy(
                    update={
                        "lesson_id": prefix + lesson.lesson_id,
                        "chapter_id": chapter_id,
                        "claims": [
                            claim.model_copy(update={"claim_id": prefix + claim.claim_id})
                            for claim in lesson.claims
                        ],
                    }
                )
            )
        for activity in fragment.activities:
            activities.append(
                activity.model_copy(
                    update={"activity_id": prefix + activity.activity_id, "chapter_id": chapter_id}
                )
            )
        for concept in fragment.concepts:
            concepts.setdefault(concept.concept_id, concept)
        for citation in fragment.citations:
            citations.setdefault(citation.citation_id, citation)
        unsupported.extend(prefix + claim_id for claim_id in fragment.unsupported_claim_ids)
    draft = CourseDraft(
        schema_version="course-draft-v1",
        job_id=job_id,
        attempt_number=attempt_number,
        title=title,
        outcomes=outcomes[:100] or [title],
        source_provenance=_provenance(source_refs),
        citations=list(citations.values()),
        concepts=list(concepts.values()),
        chapters=chapters,
        lessons=lessons,
        activities=activities,
    )
    return SourceVerificationOutput(
        schema_version="source-verification-v2",
        draft=draft,
        unsupported_claim_ids=list(dict.fromkeys(unsupported)),
    )


def _provenance(source_refs: tuple[str, ...]) -> list[SourceProvenanceRef]:
    seen: dict[str, SourceProvenanceRef] = {}
    for raw in source_refs:
        ref = json.loads(raw)
        seen.setdefault(
            ref["source_id"],
            SourceProvenanceRef(
                source_id=ref["source_id"],
                revision=ref["source_revision"],
                content_hash=ref["source_content_hash"],
            ),
        )
    return list(seen.values())


def fragment_from_draft(
    draft: CourseDraft, chapter_index: int, unsupported: tuple[str, ...] = ()
) -> ChapterVerificationOutput:
    """The fragment of one chapter (1-based) of a complete draft.

    Used where a known draft stands in for the model (deterministic golden path, tests).
    """
    chapter = draft.chapters[chapter_index - 1]
    lessons = [lesson for lesson in draft.lessons if lesson.chapter_id == chapter.chapter_id]
    activities = [a for a in draft.activities if a.chapter_id == chapter.chapter_id]
    concept_ids = {c for lesson in lessons for c in lesson.concept_ids} | {
        c for a in activities for c in a.concept_ids
    }
    claim_ids = {claim.claim_id for lesson in lessons for claim in lesson.claims}
    return ChapterVerificationOutput(
        schema_version="chapter-verification-v1",
        chapter=chapter,
        concepts=[c for c in draft.concepts if c.concept_id in concept_ids] or draft.concepts,
        citations=draft.citations,
        lessons=lessons,
        activities=activities,
        unsupported_claim_ids=[c for c in unsupported if c in claim_ids],
    )
