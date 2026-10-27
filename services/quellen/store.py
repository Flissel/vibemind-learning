"""Watched source pages in Learning's database: taken up when a course is built from them,
checked by the nightly run, and listed while a change stands unreviewed."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from spaces.learning.services.db.models import LearningSourceWatch, utc_now
from spaces.learning.services.quellen.frische import Change, Watch


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).isoformat()


class WatchStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def take_up(self, *, course_id: str, url: str, sha256: str) -> Watch:
        """Watch a page for a course, or move the watch to the page's current digest."""
        with self._sessions() as db, db.begin():
            row = db.scalar(select(LearningSourceWatch).where(
                LearningSourceWatch.course_id == course_id, LearningSourceWatch.url == url,
            ))
            if row is None:
                row = LearningSourceWatch(id=str(uuid4()), course_id=course_id, url=url, sha256=sha256, note="")
                db.add(row)
            row.sha256 = sha256
            row.changed_at = None
            row.note = ""
            db.flush()
            return Watch(id=row.id, course_id=row.course_id, url=row.url, sha256=row.sha256)

    def watches(self) -> list[Watch]:
        with self._sessions() as db:
            rows = db.scalars(select(LearningSourceWatch).order_by(LearningSourceWatch.course_id, LearningSourceWatch.url)).all()
            return [Watch(id=r.id, course_id=r.course_id, url=r.url, sha256=r.sha256) for r in rows]

    def record(self, changes: list[Change], *, now: datetime | None = None) -> int:
        """Mark every reached watch as checked now and the changed ones as changed; the digest
        the course was built from stays until someone takes the page up again. An unreachable
        page keeps the time it was last reached and gets a note instead."""
        moment = now if now is not None else utc_now()
        changed = {(c.course_id, c.url): c for c in changes}
        with self._sessions() as db, db.begin():
            rows = db.scalars(select(LearningSourceWatch)).all()
            for row in rows:
                change = changed.get((row.course_id, row.url))
                if change is None:
                    row.last_checked_at = moment
                    continue
                if change.new_sha is None:
                    note = f"Quelle nicht erreichbar ({change.error or 'unbekannt'}): bitte Kurs prüfen"
                else:
                    row.last_checked_at = moment
                    note = f"Quelle geändert (neuer Text, Digest {change.new_sha[:12]}…): bitte Kurs prüfen"
                if row.changed_at is None:
                    row.changed_at = moment
                row.note = note
            return len(changed)

    def changed(self) -> list[dict[str, Any]]:
        with self._sessions() as db:
            rows = db.scalars(
                select(LearningSourceWatch)
                .where(LearningSourceWatch.changed_at.is_not(None))
                .order_by(LearningSourceWatch.changed_at.desc())
            ).all()
            return [{
                "course_id": r.course_id, "url": r.url, "changed_at": _iso(r.changed_at),
                "last_checked_at": _iso(r.last_checked_at), "note": r.note,
            } for r in rows]
