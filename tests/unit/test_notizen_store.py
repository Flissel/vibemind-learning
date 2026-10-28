"""Notes a person takes in the player: created with the course's explanation, listed newest
first, and only the person themself may add their own text."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.db.models import Base
from spaces.learning.services.notizen.store import NoteStore


def _factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'notizen.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _note(store: NoteStore, actor_id: str = "lh-3-lernende", **changes) -> dict:
    fields = dict(
        actor_id=actor_id, course_id="c1", sequence_id="seq-roi", anchor={"section_id": "f1"},
        selection="45.000 € im Jahr", explanation="Die jährlichen Kosten laut Folie 2.",
        extra="Allgemein: ein ROI vergleicht Ertrag mit Einsatz.", extra_sources=["https://example.org/roi"],
    )
    fields.update(changes)
    return store.create(**fields)


def test_notes_are_created_and_listed_newest_first_without_the_actor(tmp_path: Path) -> None:
    store = NoteStore(_factory(tmp_path))
    first = _note(store)
    second = _note(store, anchor={"checkpoint_id": "q1", "at_s": 12.5}, selection="ROI")

    assert "actor_id" not in first
    assert first["own_text"] == "" and first["selection"] == "45.000 € im Jahr"
    assert first["extra_sources"] == ["https://example.org/roi"] and first["anchor"] == {"section_id": "f1"}
    assert [n["id"] for n in store.list_for("lh-3-lernende")] == [second["id"], first["id"]]
    assert store.list_for("lh-3-ben") == []


def test_only_the_owner_sets_their_own_text(tmp_path: Path) -> None:
    store = NoteStore(_factory(tmp_path))
    note = _note(store)

    changed = store.set_own_text(note["id"], "lh-3-lernende", "Für die Budgetrunde merken.")
    assert changed["own_text"] == "Für die Budgetrunde merken." and changed["updated_at"] >= changed["created_at"]
    assert store.list_for("lh-3-lernende")[0]["own_text"] == "Für die Budgetrunde merken."
    with pytest.raises(LookupError):
        store.set_own_text(note["id"], "lh-3-ben", "fremd")
    with pytest.raises(LookupError):
        store.set_own_text("00000000-0000-4000-8000-000000000000", "lh-3-lernende", "weg")


def test_limits_are_enforced(tmp_path: Path) -> None:
    store = NoteStore(_factory(tmp_path))
    with pytest.raises(ValueError):
        _note(store, selection="x" * 2_001)
    note = _note(store)
    with pytest.raises(ValueError):
        store.set_own_text(note["id"], "lh-3-lernende", "x" * 10_001)
