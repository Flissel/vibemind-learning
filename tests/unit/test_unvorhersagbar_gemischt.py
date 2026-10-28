"""Shown order of ordering and matching tasks: unpredictable and never the solution.

The code is public, so a permutation recomputable from a seed (item id, checkpoint id) would
hand out the answer. Ordering options and matching right columns come from a secure RNG.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from spaces.learning.services import zufall as zufall_modul
from spaces.learning.services.adaptive_engine.models import AdaptiveItem
from spaces.learning.services.course_factory.neu_mischen import reihenfolgen_neu_mischen
from spaces.learning.services.course_factory.structured_items import structured_items
from spaces.learning.services.db.models import Base
from spaces.learning.services.lernsequenz.aus_struktur import structured_checkpoints
from spaces.learning.tests.unit.test_lernsequenz_aus_struktur import BODY, THREE_SHELLS, lesson
from spaces.learning.tests.unit.test_structured_items import _draft_with_structure


class _Umkehr:
    """Stands in for the secure RNG: the first `identisch` shuffles keep the order, then it reverses."""

    def __init__(self, identisch: int = 0) -> None:
        self.aufrufe = 0
        self.identisch = identisch

    def shuffle(self, liste: list[Any]) -> None:
        self.aufrufe += 1
        if self.aufrufe > self.identisch:
            liste.reverse()


@pytest.fixture()
def media(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))


# --- helper --------------------------------------------------------------------------------


def test_helper_never_returns_the_given_order_and_keeps_all_entries() -> None:
    for n in range(2, 6):
        eintraege = [{"id": f"x{i}", "label": f"l{i}"} for i in range(n)]
        for _ in range(40):
            gezeigt = zufall_modul.anders_gemischt(eintraege)
            assert [e["id"] for e in gezeigt] != [e["id"] for e in eintraege]
            assert sorted(e["id"] for e in gezeigt) == sorted(e["id"] for e in eintraege)
    assert zufall_modul.anders_gemischt([{"id": "a"}]) == [{"id": "a"}]
    assert zufall_modul.anders_gemischt([]) == []


def test_helper_reshuffles_while_the_rng_keeps_the_order(monkeypatch) -> None:
    rng = _Umkehr(identisch=2)
    monkeypatch.setattr(zufall_modul, "ZUFALL", rng)
    eintraege = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert [e["id"] for e in zufall_modul.anders_gemischt(eintraege)] == ["c", "b", "a"]
    assert rng.aufrufe == 3


# --- ordering options (structured_items) ---------------------------------------------------


def test_ordering_options_come_from_the_secure_rng_not_from_the_activity_id(monkeypatch) -> None:
    rng = _Umkehr()
    monkeypatch.setattr(zufall_modul, "ZUFALL", rng)
    ordering = next(s for s in structured_items(_draft_with_structure()) if s.item_type == "ordering")
    shown = [o["id"] for o in ordering.scoring_config["options"]]
    assert shown == list(reversed(ordering.expected_answer["order"]))
    seeded = [{"id": i} for i in ordering.expected_answer["order"]]
    random.Random(ordering.activity_id).shuffle(seeded)
    assert shown != [o["id"] for o in seeded]


def test_ordering_options_differ_between_builds_and_never_equal_the_solution() -> None:
    seen = set()
    for _ in range(30):
        ordering = next(s for s in structured_items(_draft_with_structure()) if s.item_type == "ordering")
        shown = tuple(o["id"] for o in ordering.scoring_config["options"])
        assert list(shown) != ordering.expected_answer["order"]
        seen.add(shown)
    assert len(seen) > 1  # same seed (activity id), different shown orders


# --- matching right column (aus_struktur) --------------------------------------------------


def _aligned(matching) -> list[str]:
    return [matching.pairs[left.item_id] for left in matching.left]


def test_matching_right_column_comes_from_the_secure_rng(monkeypatch) -> None:
    rng = _Umkehr()
    monkeypatch.setattr(zufall_modul, "ZUFALL", rng)
    matching = next(c for c in structured_checkpoints(lesson(BODY), after="l1", refs=()) if c.kind == "matching")
    assert [r.item_id for r in matching.right] == list(reversed(_aligned(matching)))


def test_matching_right_column_never_equals_the_aligned_order() -> None:
    orders = set()
    for _ in range(30):
        matching = next(c for c in structured_checkpoints(lesson(BODY), after="l1", refs=()) if c.kind == "matching")
        shown = [r.item_id for r in matching.right]
        assert shown != _aligned(matching)
        orders.add(tuple(shown))
    assert len(orders) > 1


def test_window_matching_right_column_secure_and_not_aligned(media, monkeypatch) -> None:
    def windows(checkpoints):
        return next(c for c in checkpoints if c.kind == "matching" and c.title.startswith("Welches Fenster"))

    for _ in range(15):
        matching = windows(structured_checkpoints(lesson(THREE_SHELLS), after="l1-1", refs=()))
        assert [r.item_id for r in matching.right] != _aligned(matching)
    monkeypatch.setattr(zufall_modul, "ZUFALL", _Umkehr())
    matching = windows(structured_checkpoints(lesson(THREE_SHELLS), after="l1-1", refs=()))
    assert [r.item_id for r in matching.right] == list(reversed(_aligned(matching)))


# --- maintenance: reshuffle stored items ---------------------------------------------------


def _factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'mischen.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _item(item_id: str, item_type: str, expected: dict, scoring: dict) -> AdaptiveItem:
    return AdaptiveItem(
        id=item_id, course_id="c1", course_revision=1, activity_id=f"a-{item_id}", item_type=item_type,
        difficulty=0.5, quality_status="approved", expected_answer=expected, scoring_config=scoring, rubric=[],
    )


def _bestand(factory) -> None:
    ids = ["s1", "s2", "s3", "s4"]
    # Old shape: the shown order was reconstructable; here simply the solution reversed.
    ordering = _item(
        "o1", "ordering", {"order": ids},
        {"prompt": "p", "options": [{"id": i, "label": i.upper()} for i in reversed(ids)], "partial_credit": True},
    )
    links = [{"id": f"b{n}", "label": f"L{n}"} for n in (1, 2, 3)]
    pairs = {"b1": "e1", "b2": "e2", "b3": "e3"}
    matching = _item(
        "m1", "matching", {"pairs": pairs},
        {"prompt": "p", "partial_credit": True,
         "presentation": {"left": links, "right": [{"id": f"e{n}", "label": f"R{n}"} for n in (3, 1, 2)]}},
    )
    choice = _item("q1", "single_choice", {"choice_id": "a"},
                   {"prompt": "p", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]})
    with factory() as s, s.begin():
        s.add_all([ordering, matching, choice])


def _lesen(factory, item_id: str) -> AdaptiveItem:
    with factory() as s:
        return s.scalar(select(AdaptiveItem).where(AdaptiveItem.id == item_id))


def test_reshuffle_changes_shown_order_but_not_answers(tmp_path) -> None:
    factory = _factory(tmp_path)
    _bestand(factory)
    before = {i: _lesen(factory, i) for i in ("o1", "m1", "q1")}
    expected_before = {i: dict(r.expected_answer) for i, r in before.items()}

    result = reihenfolgen_neu_mischen(factory)

    assert result["ordering"] == 1 and result["matching"] == 1
    o, m, q = (_lesen(factory, i) for i in ("o1", "m1", "q1"))
    assert o.expected_answer == expected_before["o1"] and m.expected_answer == expected_before["m1"]
    assert q.expected_answer == expected_before["q1"]
    assert q.scoring_config == before["q1"].scoring_config  # choice items untouched
    shown_o = [x["id"] for x in o.scoring_config["options"]]
    assert sorted(shown_o) == ["s1", "s2", "s3", "s4"] and shown_o != o.expected_answer["order"]
    assert o.scoring_config["prompt"] == "p" and o.scoring_config["partial_credit"] is True
    right = [x["id"] for x in m.scoring_config["presentation"]["right"]]
    aligned = [m.expected_answer["pairs"][x["id"]] for x in m.scoring_config["presentation"]["left"]]
    assert sorted(right) == sorted(aligned) and right != aligned
    assert m.scoring_config["presentation"]["left"] == before["m1"].scoring_config["presentation"]["left"]


def test_reshuffle_uses_the_secure_rng_and_can_run_twice(tmp_path, monkeypatch) -> None:
    factory = _factory(tmp_path)
    _bestand(factory)
    rng = _Umkehr()
    monkeypatch.setattr(zufall_modul, "ZUFALL", rng)
    first = reihenfolgen_neu_mischen(factory)
    assert rng.aufrufe >= 2
    assert [x["id"] for x in _lesen(factory, "o1").scoring_config["options"]] == ["s4", "s3", "s2", "s1"]
    second = reihenfolgen_neu_mischen(factory)
    assert first == second
    o = _lesen(factory, "o1")
    assert [x["id"] for x in o.scoring_config["options"]] != o.expected_answer["order"]


def test_reshuffle_skips_items_that_have_no_distinct_entries(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as s, s.begin():
        s.add(_item("o2", "ordering", {"order": ["s1"]}, {"prompt": "p", "options": [{"id": "s1", "label": "A"}]}))
    result = reihenfolgen_neu_mischen(factory)
    assert result["ordering"] == 0 and result["uebersprungen"] == 1
