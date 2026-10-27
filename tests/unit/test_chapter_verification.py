"""Chapter-wise source verification: split the team's work by chapter, merge back.

Live, one verifier call rewriting the whole draft ran past the subscription's 540 s
limit; per-chapter calls stay short and are merged without a model.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from spaces.learning.services.course_factory.chapter_verification import (
    course_title,
    merge_chapters,
    split_by_chapter,
)
from spaces.learning.services.course_factory.roles.schemas import ChapterVerificationOutput

SOURCE = str(uuid4())
CHUNK = str(uuid4())
REF = json.dumps(
    {
        "source_id": SOURCE,
        "source_revision": 1,
        "source_content_hash": "a" * 64,
        "chunk_id": CHUNK,
        "chunk_content_hash": "b" * 64,
        "locator": {"heading": "Vor der Arbeit"},
        "content": "Vor jedem Einsatz wird das Werkzeug geprueft.",
    }
)


def _role_outputs() -> dict[str, object]:
    return {
        "architect": {
            "schema_version": "architect-v1",
            "audience": "Azubis",
            "prerequisites": [],
            "outcomes": ["Gefahren erkennen"],
            "chapters": ["Vor der Arbeit", "Akkus laden"],
        },
        "concept_mapper": {
            "schema_version": "concept-map-v1",
            "concepts": ["sichtpruefung", "laden"],
            "dependencies": [],
            "chapter_coverage": {"Vor der Arbeit": ["sichtpruefung"], "Akkus laden": ["laden"]},
        },
        "lesson_author": {
            "schema_version": "lesson-v1",
            "lessons": [
                {"chapter": "Vor der Arbeit", "title": "Pruefen", "body": "Pruefen."},
                {"chapter": "Akkus laden", "title": "Laden", "body": "Laden."},
            ],
        },
        "assessment_designer": {
            "schema_version": "assessment-v1",
            "activities": [
                {"concept": "sichtpruefung", "type": "open", "prompt": "Was pruefst du?"},
                {"concept": "laden", "type": "open", "prompt": "Wie laedst du?"},
                {"concept": "unbekannt", "type": "open", "prompt": "Rest?"},
            ],
        },
    }


def test_split_gives_each_chapter_its_lessons_and_activities() -> None:
    slices = split_by_chapter(_role_outputs())

    assert [s.title for s in slices] == ["Vor der Arbeit", "Akkus laden"]
    assert [lesson["title"] for lesson in slices[0].lessons] == ["Pruefen"]
    assert [a["prompt"] for a in slices[0].activities][:1] == ["Was pruefst du?"]
    assert slices[1].concepts == ["laden"]
    prompts = [a["prompt"] for s in slices for a in s.activities]
    assert sorted(prompts) == sorted(["Rest?", "Wie laedst du?", "Was pruefst du?"])


def test_split_matches_short_lesson_chapters_to_long_architect_titles() -> None:
    # Live (22./23.10.): the architect wrote "Kapitel 1: Titel. Grundlage: ... Inhalte:
    # ..." while lessons named only "Kapitel 1: Titel". Nothing matched exactly, every
    # lesson fell into the last chapter, and the merged course had one chapter.
    outputs = _role_outputs()
    outputs["architect"]["chapters"] = [
        "Kapitel 1: Vor der Arbeit – Sichtprüfung. Grundlage: 'Vor der Arbeit'. Inhalte: Risse.",
        "Kapitel 2: Akkus laden und lagern. Inhalte: Ladegeraet, Lagerung.",
    ]
    outputs["concept_mapper"]["chapter_coverage"] = {
        "Kapitel 1: Vor der Arbeit – Sichtprüfung": ["sichtpruefung"],
        "Kapitel 2: Akkus laden und lagern": ["laden"],
    }
    outputs["lesson_author"]["lessons"] = [
        {"chapter": "Kapitel 1: Vor der Arbeit – Sichtprüfung", "title": "Pruefen", "body": "."},
        {"chapter": "Kapitel 2: Akkus laden und lagern", "title": "Laden", "body": "."},
    ]

    slices = split_by_chapter(outputs)

    assert [[lesson["title"] for lesson in s.lessons] for s in slices] == [["Pruefen"], ["Laden"]]
    assert [s.concepts for s in slices] == [["sichtpruefung"], ["laden"]]
    assert [a["prompt"] for a in slices[0].activities][:1] == ["Was pruefst du?"]


def test_split_reads_slug_coverage_keys_and_multi_concept_tasks() -> None:
    # Live: coverage keys "kapitel-1", tasks naming "a / b / c"; a capstone task
    # spanning several chapters belongs to the summary chapter that teaches them all.
    outputs = _role_outputs()
    outputs["architect"]["chapters"] = ["Kapitel 1: Pruefen", "Kapitel 2: Laden", "Kapitel 3: Abschluss"]
    outputs["concept_mapper"]["chapter_coverage"] = {
        "kapitel-1": ["sichtpruefung"],
        "kapitel-2": ["laden"],
        "kapitel-3": ["sichtpruefung", "laden"],
    }
    outputs["lesson_author"]["lessons"] = [
        {"chapter": f"Kapitel {n}", "title": t, "body": "."}
        for n, t in ((1, "Pruefen"), (2, "Laden"), (3, "Abschluss"))
    ]
    outputs["assessment_designer"]["activities"] = [
        {"concept": "sichtpruefung", "type": "open", "prompt": "A"},
        {"concept": "laden", "type": "open", "prompt": "B"},
        {"concept": "sichtpruefung / laden", "type": "case", "prompt": "C"},
    ]

    slices = split_by_chapter(outputs)

    assert [s.concepts for s in slices] == [["sichtpruefung"], ["laden"], ["sichtpruefung", "laden"]]
    assert [[a["prompt"] for a in s.activities] for s in slices] == [["A"], ["B"], ["C"]]


def test_split_falls_back_to_lesson_order_when_names_differ() -> None:
    outputs = _role_outputs()
    outputs["lesson_author"]["lessons"] = [
        {"chapter": "Erstes Kapitel", "title": "Pruefen", "body": "."},
        {"chapter": "Zweites Kapitel", "title": "Laden", "body": "."},
    ]

    slices = split_by_chapter(outputs)

    assert [[lesson["title"] for lesson in s.lessons] for s in slices] == [["Pruefen"], ["Laden"]]


def _fragment(chapter_id: str, concept: str, claim_ok: bool = True) -> ChapterVerificationOutput:
    citation_id = str(uuid4())
    return ChapterVerificationOutput.model_validate(
        {
            "schema_version": "chapter-verification-v1",
            "chapter": {
                "chapter_id": chapter_id,
                "title": f"Kapitel {chapter_id}",
                "lesson_ids": ["lesson-1"],
                "activity_ids": ["activity-1"],
            },
            "concepts": [{"concept_id": concept, "title": concept.title()}],
            "citations": [
                {
                    "citation_id": citation_id,
                    "source_id": SOURCE,
                    "source_revision": 1,
                    "chunk_id": CHUNK,
                    "content_hash": "b" * 64,
                    "locator": {"heading": "Vor der Arbeit"},
                }
            ],
            "lessons": [
                {
                    "lesson_id": "lesson-1",
                    "chapter_id": chapter_id,
                    "title": "Lektion",
                    "body": "Text.",
                    "concept_ids": [concept],
                    "claims": [
                        {"claim_id": "claim-1", "text": "Fakt.", "citation_ids": [citation_id]}
                    ],
                }
            ],
            "activities": [
                {
                    "activity_id": "activity-1",
                    "chapter_id": chapter_id,
                    "type": "open",
                    "concept_ids": [concept],
                    "prompt": "Frage?",
                    "expected_answer": "Antwort.",
                    "citation_ids": [citation_id],
                    "rubric": [{"criterion_id": "c", "description": "korrekt", "points": 1}],
                }
            ],
            "unsupported_claim_ids": [] if claim_ok else ["claim-1"],
        }
    )


def test_merge_builds_one_valid_draft_with_unique_ids() -> None:
    job_id = str(uuid4())
    merged = merge_chapters(
        [_fragment("ch", "sichtpruefung"), _fragment("ch", "laden", claim_ok=False)],
        job_id=job_id,
        attempt_number=2,
        title="Akku-Werkzeug sicher nutzen",
        outcomes=["Gefahren erkennen"],
        source_refs=(REF,),
    )

    draft = merged.draft
    assert draft.job_id == job_id and draft.attempt_number == 2
    assert draft.title == "Akku-Werkzeug sicher nutzen"
    assert [c.chapter_id for c in draft.chapters] == ["k1-ch", "k2-ch"]
    assert len({lesson.lesson_id for lesson in draft.lessons}) == 2
    assert draft.chapters[1].lesson_ids == ["k2-lesson-1"]
    assert draft.activities[1].chapter_id == "k2-ch"
    assert {c.concept_id for c in draft.concepts} == {"sichtpruefung", "laden"}
    assert [p.source_id for p in draft.source_provenance] == [SOURCE]
    assert merged.unsupported_claim_ids == ["k2-claim-1"]
    assert draft.lessons[1].claims[0].claim_id == "k2-claim-1"


def test_merge_refuses_a_fragment_that_references_foreign_ids() -> None:
    broken = _fragment("ch", "laden").model_copy(deep=True)
    broken.chapter.lesson_ids = ["lesson-404"]
    with pytest.raises(ValueError):
        merge_chapters(
            [broken],
            job_id=str(uuid4()),
            attempt_number=1,
            title="T",
            outcomes=["o"],
            source_refs=(REF,),
        )


def test_chapter_prompt_keeps_concept_ids_and_demands_a_task_per_concept() -> None:
    # Live: every chapter verifier invented its own concept ids (con-..., concept-...,
    # <course>-ch04-c01) and left taught concepts without a task in its chapter.
    from spaces.learning.services.course_factory.model_gateway import (
        ROLE_CONTRACTS,
        ROLE_PROMPTS,
    )

    prompt = ROLE_PROMPTS["chapter_verifier"]
    assert "exactly the strings of the input concepts" in prompt
    assert "at least one activity in this chapter" in prompt
    assert ROLE_CONTRACTS["chapter_verifier"][1] == "chapter-verification-prompt-v5"


def test_task_prompts_carry_the_ai_review_lessons() -> None:
    # Live (22.10.): the AI reviewer sent back three attempts for an expected answer
    # demanding a fact the scenario did not need, authoring notes inside rubrics,
    # a capstone chapter with a single task, and free-text concept ids.
    from spaces.learning.services.course_factory.model_gateway import (
        ROLE_CONTRACTS,
        ROLE_PROMPTS,
    )

    for role in ("assessment_designer", "chapter_verifier"):
        prompt = ROLE_PROMPTS[role]
        assert "only what the task's own scenario requires" in prompt
        assert "no authoring notes" in prompt
        assert "summary or capstone chapter" in prompt
    assert "short lowercase slugs" in ROLE_PROMPTS["concept_mapper"]
    assert ROLE_CONTRACTS["assessment_designer"][1] == "assessment-prompt-v9"
    assert ROLE_CONTRACTS["concept_mapper"][1] == "concept-map-prompt-v2"


def test_course_title_comes_from_the_generation_request() -> None:
    assert course_title("Course: Akku sicher\nLevel: beginner", fallback="x") == "Akku sicher"
    assert course_title("ohne Titel", fallback="Erstes Kapitel") == "Erstes Kapitel"


def test_task_prompts_create_and_keep_drawing_tasks() -> None:
    # Canvas tasks are graded from the drawn image; the factory must create them when an
    # objective asks for a drawing, and the chapter verifier must not turn them into text.
    from spaces.learning.services.course_factory.model_gateway import ROLE_PROMPTS

    assert "penecho_canvas" in ROLE_PROMPTS["assessment_designer"]
    assert "draw" in ROLE_PROMPTS["assessment_designer"]
    assert "Keep penecho_canvas activities" in ROLE_PROMPTS["chapter_verifier"]
