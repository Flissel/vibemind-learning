"""Build an interactive presentation from a verified course draft, without a model.

Every lesson becomes one slide per section (``##`` heading), bullet lists become slide
points, the prose becomes the narration. The chapter's choice questions become
checkpoints after its last slide. Only what the draft already says (and cites) is used.
"""

from __future__ import annotations

import re

from spaces.learning.contracts.lernsequenz import (
    Choice,
    LernsequenzV1,
    Option,
    Slide,
    SourceNote,
    VideoSegment,
)
from spaces.learning.services.course_factory.schemas import ActivityDraft, CourseDraft, LessonDraft

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_TOKEN = re.compile(r"[^A-Za-z0-9._-]+")


def _token(value: str, limit: int = 60) -> str:
    cleaned = _TOKEN.sub("-", value).strip("-.") or "x"
    return cleaned[:limit]


def _plain(text: str) -> str:
    return re.sub(r"(\*\*|__|`)", "", text).strip()


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _sections(lesson: LessonDraft) -> list[tuple[str, list[str], list[str]]]:
    """(title, points, prose) per ``##`` section; the lesson title for the part before."""
    parts: list[tuple[str, list[str], list[str]]] = [(lesson.title, [], [])]
    for raw in lesson.body.splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _HEADING.match(line)
        if heading:
            if len(heading.group(1)) == 1 and not parts[-1][1] and not parts[-1][2]:
                parts[-1] = (_plain(heading.group(2)), [], [])
            else:
                parts.append((_plain(heading.group(2)), [], []))
            continue
        bullet = _BULLET.match(line)
        if bullet:
            parts[-1][1].append(_plain(bullet.group(1)))
        else:
            parts[-1][2].append(_plain(line))
    return [part for part in parts if part[1] or part[2]]


def _slides(lesson: LessonDraft) -> list[Slide]:
    refs = tuple(dict.fromkeys(ref for claim in lesson.claims for ref in claim.citation_ids))[:20]
    slides = []
    for index, (title, points, prose) in enumerate(_sections(lesson), start=1):
        text = " ".join(prose)
        first_sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""
        slides.append(Slide(
            section_id=_token(f"{lesson.lesson_id}-{index}", 120),
            title=_cut(title, 300),
            statement=_cut(first_sentence, 300) if first_sentence and not points else None,
            points=tuple(_cut(point, 300) for point in points[:8]),
            narration=_cut(text or " · ".join(points), 2_000),
            refs=refs,
        ))
    return slides


def _choice(activity: ActivityDraft, after: str) -> Choice | None:
    if activity.type != "quiz" or len(activity.options) < 2 or not activity.correct_option_ids:
        return None
    correct = [o.text for o in activity.options if o.option_id in activity.correct_option_ids]
    rationale = activity.expected_answer.strip() or "Richtig: " + "; ".join(correct)
    return Choice(
        checkpoint_id=_token(f"q-{activity.activity_id}", 120),
        after_section=after,
        title="Kurz geprüft",
        question=_cut(activity.prompt, 2_000),
        options=tuple(
            Option(option_id=_token(o.option_id, 50), text=_cut(o.text, 300))
            for o in activity.options[:6]
        ),
        correct=tuple(_token(o, 50) for o in activity.correct_option_ids),
        rationale=_cut(rationale, 2_000),
        refs=tuple(activity.citation_ids[:20]),
        activity_id=activity.activity_id,
    )


def _source_title(locator: dict[str, str | int]) -> str:
    for key in ("heading", "section", "title"):
        if key in locator:
            return _cut(str(locator[key]), 300)
    if "page" in locator:
        return f"Seite {locator['page']}"
    return "Kursmaterial"


def as_video(presentation: LernsequenzV1, *, media_name: str, slide_times: dict[str, dict[str, float]]) -> LernsequenzV1:
    """The presentation's explainer video: one video section, checkpoints at the slide ends.

    ``slide_times`` comes from ``video_bau`` (start/end in seconds per slide section).
    """
    if presentation.kind != "presentation":
        raise ValueError("only a presentation can become a video")
    missing = {s.section_id for s in presentation.sections} - set(slide_times)
    if missing:
        raise ValueError("slide times are missing for some slides")
    refs = tuple(dict.fromkeys(r for s in presentation.sections for r in s.refs))[:20]
    transcript = " ".join(
        s.narration for s in presentation.sections if isinstance(s, Slide)
    )
    video = VideoSegment(
        section_id="video",
        title=presentation.title,
        url=f"/lernsequenz-medien/{media_name}",
        end_s=max(t["end"] for t in slide_times.values()),
        transcript=_cut(transcript, 2_000),
        refs=refs,
    )
    checkpoints = tuple(
        checkpoint.model_copy(update={
            "after_section": "video",
            "at_s": max(0.0, slide_times[checkpoint.after_section]["end"] - 0.4),
        })
        for checkpoint in presentation.checkpoints
    )
    return LernsequenzV1(
        # Always its own id: the video must never overwrite its presentation.
        sequence_id=_token("video-" + presentation.sequence_id.removeprefix("praes-"), 120),
        course_id=presentation.course_id,
        title=presentation.title,
        kind="video",
        sections=(video,),
        checkpoints=checkpoints,
        sources=presentation.sources,
    )


def build_presentation(draft: CourseDraft, *, course_id: str, chapter_id: str | None = None) -> LernsequenzV1:
    chapters = [c for c in draft.chapters if chapter_id in (None, c.chapter_id)]
    if not chapters:
        raise LookupError("chapter is unknown")
    lessons = {lesson.lesson_id: lesson for lesson in draft.lessons}
    activities = {activity.activity_id: activity for activity in draft.activities}
    sections: list[Slide] = []
    checkpoints: list[Choice] = []
    for chapter in chapters:
        chapter_slides = [
            slide for lesson_id in chapter.lesson_ids if lesson_id in lessons
            for slide in _slides(lessons[lesson_id])
        ]
        if not chapter_slides:
            continue
        sections.extend(chapter_slides)
        for activity_id in chapter.activity_ids:
            checkpoint = _choice(activities[activity_id], chapter_slides[-1].section_id) if activity_id in activities else None
            if checkpoint is not None:
                checkpoints.append(checkpoint)
    used = {ref for item in [*sections, *checkpoints] for ref in item.refs}
    sources = tuple(
        SourceNote(ref=c.citation_id, title=_source_title(c.locator))
        for c in draft.citations if c.citation_id in used
    )
    suffix = _token(chapter_id) if chapter_id else "kurs"
    return LernsequenzV1(
        sequence_id=_token(f"praes-{course_id}-{suffix}", 120),
        course_id=course_id,
        title=_cut(draft.title if chapter_id is None else chapters[0].title, 300),
        kind="presentation",
        sections=tuple(sections),
        checkpoints=tuple(checkpoints),
        sources=sources,
    )
