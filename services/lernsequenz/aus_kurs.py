"""Build an interactive presentation from a verified course draft, without a model.

Every lesson becomes one slide per section (``##`` heading), bullet lists become slide
points, the prose becomes the narration. The lesson's numbered lists, command lines and
term lists become hands-on checkpoints after its last slide (``aus_struktur``); the
chapter's choice questions follow after its last slide. Only what the draft already says
(and cites) is used.
"""

from __future__ import annotations

import re

from spaces.learning.contracts.lernsequenz import (
    Checkpoint,
    Choice,
    Cloze,
    ImageCloze,
    Condition,
    LernsequenzV1,
    Option,
    Slide,
    SourceNote,
    VideoSegment,
)
from spaces.learning.services.course_factory.schemas import ActivityDraft, CourseDraft, LessonDraft
from spaces.learning.services.lernsequenz.aus_struktur import profile_checkpoint, shell_condition, structured_checkpoints
from spaces.learning.services.lernsequenz.text import cut as _cut, plain as _plain, token as _token

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


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


# Short sections (no list, little prose) share a slide: one point each. Generated lessons
# have many one-sentence sections; one slide each made a 2-hour course 51 slides long.
_SHORT_PROSE = 320
_POINTS_PER_SLIDE = 5


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""


def _groups(sections: list[tuple[str, list[str], list[str]]]) -> list[list[tuple[str, list[str], list[str]]]]:
    groups: list[list[tuple[str, list[str], list[str]]]] = []
    current: list[tuple[str, list[str], list[str]]] = []
    for section in sections:
        _title, points, prose = section
        if points or len(" ".join(prose)) > _SHORT_PROSE:
            if current:
                groups.append(current)
                current = []
            groups.append([section])
            continue
        current.append(section)
        if len(current) == _POINTS_PER_SLIDE:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _slides(lesson: LessonDraft) -> list[Slide]:
    refs = tuple(dict.fromkeys(ref for claim in lesson.claims for ref in claim.citation_ids))[:20]
    slides = []
    for index, group in enumerate(_groups(_sections(lesson)), start=1):
        section_id = _token(f"{lesson.lesson_id}-{index}", 120)
        if len(group) == 1:
            title, points, prose = group[0]
            text = " ".join(prose)
            first_sentence = _first_sentence(text)
            slides.append(Slide(
                section_id=section_id,
                title=_cut(title, 300),
                statement=_cut(first_sentence, 300) if first_sentence and not points else None,
                points=tuple(_cut(point, 300) for point in points[:8]),
                narration=_cut(text or " · ".join(points), 2_000),
                refs=refs,
            ))
            continue
        slides.append(Slide(
            section_id=section_id,
            title=_cut(lesson.title, 300),
            points=tuple(
                _cut(f"{title}: {_first_sentence(' '.join(prose))}" if prose else title, 300)
                for title, _points, prose in group
            ),
            narration=_cut(" ".join(" ".join(prose) for _title, _points, prose in group), 2_000),
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


def build_presentation(
    draft: CourseDraft, *, course_id: str, chapter_id: str | None = None, profile: bool = False,
) -> LernsequenzV1:
    """``profile``: ask "Dein Weg" (shell) after the first slide; slides whose title names a
    shell and command blanks with a shell are then shown only for that answer."""
    chapters = [c for c in draft.chapters if chapter_id in (None, c.chapter_id)]
    if not chapters:
        raise LookupError("chapter is unknown")
    lessons = {lesson.lesson_id: lesson for lesson in draft.lessons}
    activities = {activity.activity_id: activity for activity in draft.activities}
    sections: list[Slide] = []
    checkpoints: list[Checkpoint] = []
    for chapter in chapters:
        chapter_slides: list[Slide] = []
        structured: list[Checkpoint] = []
        for lesson_id in chapter.lesson_ids:
            slides = _slides(lessons[lesson_id]) if lesson_id in lessons else []
            if not slides:
                continue
            chapter_slides.extend(slides)
            structured.extend(structured_checkpoints(lessons[lesson_id], after=slides[-1].section_id, refs=slides[-1].refs))
        if not chapter_slides:
            continue
        sections.extend(chapter_slides)
        checkpoints.extend(structured)
        for activity_id in chapter.activity_ids:
            checkpoint = _choice(activities[activity_id], chapter_slides[-1].section_id) if activity_id in activities else None
            if checkpoint is not None:
                checkpoints.append(checkpoint)
    if profile and sections:
        sections = [slide.model_copy(update={"only_for": shell_condition(slide.title)}) for slide in sections]
        checkpoints = [
            c.model_copy(update={"only_for": (Condition(key="shell", option=c.shell),)}) if isinstance(c, (Cloze, ImageCloze)) and c.shell else c
            for c in checkpoints
        ]
        checkpoints.insert(0, profile_checkpoint(sections[0].section_id))
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
