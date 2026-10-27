"""Notes per person: what was marked, how the course explains it, what the tutor added
from outside the course (kept apart, with sources), and the person's own words.

Only the learner id a note was created for can read or change it; the id itself never
leaves the store in a reply (LearnHouse knows who it asks for).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from spaces.learning.services.db.models import LearningNote, utc_now

SELECTION_MAX = 2_000
TEXT_MAX = 20_000
OWN_TEXT_MAX = 10_000
SOURCES_MAX = 20


def _text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be text of at most {limit} characters")
    return value


def _iso(moment: datetime) -> str:
    # SQLite hands timestamps back naive; they were written in UTC.
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).isoformat()


def _public(note: LearningNote) -> dict[str, Any]:
    return {
        "id": note.id,
        "course_id": note.course_id,
        "sequence_id": note.sequence_id,
        "anchor": dict(note.anchor),
        "selection": note.selection,
        "explanation": note.explanation,
        "extra": note.extra,
        "extra_sources": list(note.extra_sources),
        "own_text": note.own_text,
        "created_at": _iso(note.created_at),
        "updated_at": _iso(note.updated_at),
    }


class NoteStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def create(
        self, *, actor_id: str, course_id: str, sequence_id: str, anchor: dict[str, Any],
        selection: str, explanation: str, extra: str, extra_sources: list[str],
    ) -> dict[str, Any]:
        if not isinstance(anchor, dict) or not anchor:
            raise ValueError("anchor must name a section or checkpoint")
        if not isinstance(extra_sources, list) or len(extra_sources) > SOURCES_MAX or not all(
            isinstance(s, str) and 0 < len(s) <= 500 for s in extra_sources
        ):
            raise ValueError("extra_sources must be a short list of texts")
        note = LearningNote(
            id=str(uuid4()),
            actor_id=_text(actor_id, "actor_id", 128),
            course_id=_text(course_id, "course_id", 128),
            sequence_id=_text(sequence_id, "sequence_id", 128),
            anchor=anchor,
            selection=_text(selection, "selection", SELECTION_MAX),
            explanation=_text(explanation, "explanation", TEXT_MAX),
            extra=_text(extra, "extra", TEXT_MAX),
            extra_sources=list(extra_sources),
            own_text="",
        )
        now = utc_now()
        note.created_at = note.updated_at = now
        with self._sessions() as db, db.begin():
            db.add(note)
        return _public(note)

    def list_for(self, actor_id: str) -> list[dict[str, Any]]:
        with self._sessions() as db:
            notes = db.scalars(
                select(LearningNote)
                .where(LearningNote.actor_id == actor_id)
                .order_by(LearningNote.created_at.desc(), LearningNote.id.desc())
            ).all()
            return [_public(note) for note in notes]

    def set_own_text(self, note_id: str, actor_id: str, text: str) -> dict[str, Any]:
        own_text = _text(text, "own_text", OWN_TEXT_MAX)
        with self._sessions() as db, db.begin():
            note = db.scalar(select(LearningNote).where(LearningNote.id == note_id, LearningNote.actor_id == actor_id))
            if note is None:
                raise LookupError("note not found")
            note.own_text = own_text
            note.updated_at = utc_now()
            db.flush()
            return _public(note)
