"""A verified course draft becomes an interactive presentation without a model."""

from __future__ import annotations

import json

import pytest

from spaces.learning.contracts.lernsequenz import public_view
from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.lernsequenz.aus_kurs import as_video, build_presentation

COURSE = "b189a3ec-a82a-4ae2-8cab-e71ab0d75055"
CIT = "11111111-1111-4111-8111-111111111101"


def draft() -> CourseDraft:
    return CourseDraft.model_validate({
        "schema_version": "course-draft-v1",
        "job_id": "22222222-2222-4222-8222-222222222222",
        "attempt_number": 1,
        "title": "Sicher bohren",
        "outcomes": ["Sicher bohren"],
        "source_provenance": [{"source_id": "33333333-3333-4333-8333-333333333333",
                               "revision": 1, "content_hash": "a" * 64}],
        "citations": [{"citation_id": CIT, "source_id": "33333333-3333-4333-8333-333333333333",
                       "source_revision": 1, "chunk_id": "44444444-4444-4444-8444-444444444444",
                       "content_hash": "b" * 64, "locator": {"heading": "Vor der Arbeit"}}],
        "concepts": [{"concept_id": "pruefen", "title": "Prüfen"}],
        "chapters": [{"chapter_id": "k1", "title": "Vor der Arbeit", "lesson_ids": ["l1"],
                      "activity_ids": ["a1", "a2"]}],
        "lessons": [{
            "lesson_id": "l1", "chapter_id": "k1", "title": "Gerät prüfen",
            "body": "# Gerät prüfen\n\nVor jedem Einsatz wird das Gerät geprüft. Das dauert eine Minute.\n\n"
                    "## Sichtprüfung\n\n- Gehäuse ohne Risse\n- **Akku** fest eingerastet\n",
            "concept_ids": ["pruefen"],
            "claims": [{"claim_id": "c1", "text": "Vor jedem Einsatz prüfen.", "citation_ids": [CIT]}],
        }],
        "activities": [
            {"activity_id": "a1", "chapter_id": "k1", "type": "quiz", "concept_ids": ["pruefen"],
             "prompt": "Wann wird geprüft?", "expected_answer": "Vor jedem Einsatz.",
             "citation_ids": [CIT], "rubric": [],
             "options": [{"option_id": "x", "text": "Einmal im Jahr"},
                         {"option_id": "y", "text": "Vor jedem Einsatz"}],
             "correct_option_ids": ["y"]},
            {"activity_id": "a2", "chapter_id": "k1", "type": "open", "concept_ids": ["pruefen"],
             "prompt": "Beschreibe die Prüfung.", "expected_answer": "Sichtprüfung.",
             "citation_ids": [CIT], "rubric": [{"criterion_id": "r", "description": "d", "points": 1}]},
        ],
    })


def test_lessons_become_slides_and_quizzes_checkpoints() -> None:
    sequence = build_presentation(draft(), course_id=COURSE)

    assert [s.title for s in sequence.sections] == ["Gerät prüfen", "Sichtprüfung"]
    assert sequence.sections[0].statement == "Vor jedem Einsatz wird das Gerät geprüft."
    assert sequence.sections[1].points == ("Gehäuse ohne Risse", "Akku fest eingerastet")
    assert all(s.refs == (CIT,) for s in sequence.sections)
    assert len(sequence.checkpoints) == 1  # the open task stays a practice task
    checkpoint = sequence.checkpoints[0]
    assert checkpoint.after_section == sequence.sections[-1].section_id
    assert checkpoint.correct == ("y",) and checkpoint.rationale == "Vor jedem Einsatz."
    assert sequence.sources[0].title == "Vor der Arbeit"


def test_short_sections_share_a_slide_and_keep_their_text() -> None:
    # Live (26.10.): a setup course with one-sentence sections became 51 slides.
    data = draft().model_dump(mode="json")
    short = "".join(f"## Teil {i}\n\nSatz {i} ist kurz. Noch ein Satz {i}.\n\n" for i in range(1, 8))
    data["lessons"][0]["body"] = "# Gerät prüfen\n\n" + short + "## Liste\n\n- eins\n- zwei\n"
    sequence = build_presentation(CourseDraft.model_validate(data), course_id=COURSE)

    titles = [s.title for s in sequence.sections]
    assert titles == ["Gerät prüfen", "Gerät prüfen", "Liste"]
    assert sequence.sections[0].points[0] == "Teil 1: Satz 1 ist kurz."
    assert len(sequence.sections[0].points) == 5 and len(sequence.sections[1].points) == 2
    narration = " ".join(s.narration for s in sequence.sections[:2])
    assert all(f"Noch ein Satz {i}." in narration for i in range(1, 8))
    assert sequence.sections[2].points == ("eins", "zwei")


def test_the_presentation_becomes_a_video_with_checkpoints_at_slide_ends() -> None:
    presentation = build_presentation(draft(), course_id=COURSE)
    times = {s.section_id: {"start": i * 10.0, "end": i * 10.0 + 9.5} for i, s in enumerate(presentation.sections)}

    video = as_video(presentation, media_name="akku.mp4", slide_times=times)

    assert video.kind == "video" and video.sequence_id == "video-" + presentation.sequence_id.removeprefix("praes-")
    renamed = presentation.model_copy(update={"sequence_id": "beispiel"})
    assert as_video(renamed, media_name="a.mp4", slide_times=times).sequence_id == "video-beispiel"
    section = video.sections[0]
    assert section.url == "/lernsequenz-medien/akku.mp4" and section.end_s == 19.5
    assert video.checkpoints[0].after_section == "video" and video.checkpoints[0].at_s == 19.1
    assert video.checkpoints[0].correct == ("y",)  # solutions stay on the server side
    with pytest.raises(ValueError):
        as_video(presentation, media_name="akku.mp4", slide_times={})


def test_the_built_presentation_hides_answers_and_rejects_unknown_chapters() -> None:
    sequence = build_presentation(draft(), course_id=COURSE, chapter_id="k1")
    text = json.dumps(public_view(sequence))
    assert "Vor jedem Einsatz." not in text and '"correct"' not in text
    with pytest.raises(LookupError):
        build_presentation(draft(), course_id=COURSE, chapter_id="nope")
