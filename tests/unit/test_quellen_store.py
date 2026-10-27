"""Watched source pages per course: taken up with a digest, checked, changes recorded."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.db.models import Base
from spaces.learning.services.quellen.frische import Change, Watch
from spaces.learning.services.quellen.store import WatchStore


def _store(tmp_path: Path) -> WatchStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'quellen.db'}")
    Base.metadata.create_all(engine)
    return WatchStore(sessionmaker(bind=engine, expire_on_commit=False))


def test_watches_are_taken_up_once_per_course_and_url(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.take_up(course_id="c1", url="https://docs.example/install", sha256="a" * 64)
    again = store.take_up(course_id="c1", url="https://docs.example/install", sha256="b" * 64)
    other = store.take_up(course_id="c2", url="https://docs.example/install", sha256="c" * 64)

    assert first.id == again.id and again.sha256 == "b" * 64 and other.id != first.id
    assert [w.url for w in store.watches()] == ["https://docs.example/install"] * 2


def test_changes_are_recorded_and_listed_until_taken_up_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.take_up(course_id="c1", url="https://docs.example/install", sha256="a" * 64)
    now = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)

    store.record([Change(course_id="c1", url="https://docs.example/install", old_sha="a" * 64, new_sha="d" * 64)], now=now)
    changed = store.changed()
    assert len(changed) == 1 and changed[0]["course_id"] == "c1" and changed[0]["changed_at"].startswith("2026-10-27T03:00")
    assert "Quelle" in changed[0]["note"]
    watch = store.watches()[0]
    assert watch.sha256 == "a" * 64   # the course was built from this; only a new take-up moves it
    assert store.record([], now=now) == 0

    store.take_up(course_id="c1", url="https://docs.example/install", sha256="d" * 64)
    assert store.changed() == []
