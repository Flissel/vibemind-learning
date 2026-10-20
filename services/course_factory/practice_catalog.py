"""Project a published course draft into the adaptive practice catalog.

Practice sessions read concepts and items per (course, course revision) from
Learning's own tables; nothing else writes them for factory courses. The
projection is deterministic and idempotent per course revision.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ItemConcept,
)
from spaces.learning.services.course_factory.schemas import ActivityDraft, CourseDraft
from spaces.learning.services.ingestion.models import LearningSourceChunk


SessionFactory = Callable[[], Session]

# Every item starts mid-scale; the engine selects by learner mastery.
STARTING_DIFFICULTY = 0.5
# Quiz activities carry no structured options yet, so they are graded by
# rubric against the expected answer, like open questions.
_ITEM_TYPES = {
    "quiz": "open",
    "open": "open",
    "case": "case",
    "code": "code",
    "penecho_canvas": "penecho_canvas",
}
_FALLBACK_CRITERION = {
    "criterion_id": "matches-expected-answer",
    "description": "The answer states what the expected answer states.",
    "points": 1,
}


class PracticeCatalogProjector:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def project(self, *, course_id: str, course_revision: int, draft: CourseDraft) -> int:
        """Write concepts, items and their links; return the number of items written."""
        with self._session_factory() as session, session.begin():
            exists = session.scalar(
                select(AdaptiveConcept.id)
                .where(
                    AdaptiveConcept.course_id == course_id,
                    AdaptiveConcept.course_revision == course_revision,
                )
                .limit(1)
            )
            if exists is not None:
                return 0
            concept_ids = {
                concept.concept_id: _stable_id(course_id, course_revision, "concept", concept.concept_id)
                for concept in draft.concepts
            }
            for concept in draft.concepts:
                session.add(
                    AdaptiveConcept(
                        id=concept_ids[concept.concept_id],
                        course_id=course_id,
                        course_revision=course_revision,
                        concept_key=concept.concept_id,
                        title=concept.title,
                    )
                )
            session.flush()
            refs = _source_refs(session, draft)
            written = 0
            for activity in draft.activities:
                linked = [concept_ids[c] for c in activity.concept_ids if c in concept_ids]
                if not linked:
                    continue
                item_id = _stable_id(course_id, course_revision, "item", activity.activity_id)
                session.add(_item(item_id, course_id, course_revision, activity, refs))
                session.flush()
                for concept_id in dict.fromkeys(linked):
                    session.add(
                        ItemConcept(
                            item_id=item_id,
                            concept_id=concept_id,
                            course_id=course_id,
                            course_revision=course_revision,
                        )
                    )
                written += 1
            session.flush()
            return written


def _choice(activity: ActivityDraft) -> tuple[str, dict[str, object]] | None:
    """A structured quiz becomes a choice item the engine scores without a model."""
    if activity.type != "quiz" or len(activity.options) < 2 or not activity.correct_option_ids:
        return None
    if len(activity.correct_option_ids) == 1:
        return "single_choice", {"choice_id": activity.correct_option_ids[0]}
    return "multiple_choice", {"choice_ids": list(activity.correct_option_ids)}


def _item(
    item_id: str,
    course_id: str,
    course_revision: int,
    activity: ActivityDraft,
    refs: dict[str, str],
) -> AdaptiveItem:
    rubric = [criterion.model_dump(mode="json") for criterion in activity.rubric]
    cited = [refs[c] for c in activity.citation_ids if c in refs]
    choice = _choice(activity)
    return AdaptiveItem(
        id=item_id,
        course_id=course_id,
        course_revision=course_revision,
        activity_id=activity.activity_id,
        item_type=choice[0] if choice else _ITEM_TYPES[activity.type],
        difficulty=STARTING_DIFFICULTY,
        # The draft passed the factory quality gate before it could be published.
        quality_status="approved",
        expected_answer=choice[1] if choice else {"text": activity.expected_answer or activity.prompt},
        scoring_config={
            "prompt": activity.prompt,
            "source_refs": cited,
            "hints": _section_hints(cited),
            "options": [
                {"id": option.option_id, "label": option.text} for option in activity.options
            ]
            if choice
            else [],
            "presentation": {},
        },
        # Choice items are scored deterministically and carry no rubric.
        rubric=[] if choice else (rubric or [dict(_FALLBACK_CRITERION)]),
    )


def _section_hints(refs: list[str]) -> list[str]:
    """Point to the cited section; never the answer, no model call."""
    headings: list[str] = []
    for ref in refs:
        try:
            heading = json.loads(ref).get("locator", {}).get("heading")
        except (ValueError, AttributeError):
            continue
        if isinstance(heading, str) and heading.strip() and heading not in headings:
            headings.append(heading.strip())
    return [f"Lies noch einmal den Abschnitt „{heading}“ in der Unterlage." for heading in headings]


def _source_refs(session: Session, draft: CourseDraft) -> dict[str, str]:
    """Citation id -> the same canonical source reference the factory models saw."""
    source_hashes = {ref.source_id: ref.content_hash for ref in draft.source_provenance}
    chunk_ids = {citation.chunk_id for citation in draft.citations}
    chunks = {
        chunk.id: chunk
        for chunk in session.scalars(
            select(LearningSourceChunk).where(LearningSourceChunk.id.in_(chunk_ids))
        )
    }
    refs: dict[str, str] = {}
    for citation in draft.citations:
        chunk = chunks.get(citation.chunk_id)
        if chunk is None or citation.source_id not in source_hashes:
            continue
        refs[citation.citation_id] = json.dumps(
            {
                "source_id": citation.source_id,
                "source_revision": citation.source_revision,
                "source_content_hash": source_hashes[citation.source_id],
                "chunk_id": chunk.id,
                "chunk_content_hash": chunk.content_hash,
                "locator": chunk.locator,
                "content": chunk.content,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    return refs


def _stable_id(course_id: str, course_revision: int, kind: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"learning-practice:{course_id}:{course_revision}:{kind}:{key}"))
