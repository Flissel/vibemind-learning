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
    for n in range(3, 6):
        eintraege = [{"id": f"x{i}", "label": f"l{i}"} for i in range(n)]
        for _ in range(40):
            gezeigt = zufall_modul.anders_gemischt(eintraege)
            assert [e["id"] for e in gezeigt] != [e["id"] for e in eintraege]
            assert sorted(e["id"] for e in gezeigt) == sorted(e["id"] for e in eintraege)
    assert zufall_modul.anders_gemischt([{"id": "a"}]) == [{"id": "a"}]
    assert zufall_modul.anders_gemischt([]) == []


def test_zwei_eintraege_werden_gleichverteilt_gemischt(monkeypatch) -> None:
    # "Never the solution" with two entries would name the answer: the other order.
    eintraege = [{"id": "a"}, {"id": "b"}]
    rng = _Umkehr(identisch=5)  # leaves the order unchanged: must be accepted
    monkeypatch.setattr(zufall_modul, "ZUFALL", rng)
    assert [e["id"] for e in zufall_modul.anders_gemischt(eintraege)] == ["a", "b"]
    assert rng.aufrufe == 1
    monkeypatch.undo()
    seen = {tuple(e["id"] for e in zufall_modul.anders_gemischt(eintraege)) for _ in range(60)}
    assert seen == {("a", "b"), ("b", "a")}


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
    assert sorted(x["id"] for x in q.scoring_config["options"]) == ["a", "b"] and q.scoring_config["prompt"] == "p"
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


def test_reshuffle_skips_matching_with_non_injective_pairs(tmp_path) -> None:
    factory = _factory(tmp_path)
    links = [{"id": f"b{n}", "label": f"L{n}"} for n in (1, 2, 3)]
    rechts = [{"id": f"e{n}", "label": f"R{n}"} for n in (3, 1, 2)]
    with factory() as s, s.begin():
        # b1 and b2 both point at e1: aligned ids would duplicate e1 and drop e2.
        s.add(_item("m2", "matching", {"pairs": {"b1": "e1", "b2": "e1", "b3": "e3"}},
                    {"prompt": "p", "presentation": {"left": links, "right": rechts}}))
    result = reihenfolgen_neu_mischen(factory)
    assert result["matching"] == 0 and result["uebersprungen"] == 1
    assert [x["id"] for x in _lesen(factory, "m2").scoring_config["presentation"]["right"]] == ["e3", "e1", "e2"]


def test_reshuffle_counts_malformed_rows_as_skipped_and_goes_on(tmp_path) -> None:
    factory = _factory(tmp_path)
    _bestand(factory)
    with factory() as s, s.begin():
        s.add(_item("bad1", "ordering", {"order": ["s1", "s2", "s3"]}, {"options": [{"label": "no id"}] * 3}))
        s.add(_item("bad2", "matching", {"pairs": "kaputt"}, {"presentation": {"left": 1, "right": None}}))
        s.add(_item("bad3", "single_choice", {"choice_id": "a"}, {"options": ["a", "b", "c"]}))
        s.add(_item("bad4", "ordering", {"order": ["s1", "s2", "s3"]}, {"options": [{"id": ["x"]}] * 3}))
    result = reihenfolgen_neu_mischen(factory)
    assert result["ordering"] == 1 and result["matching"] == 1
    assert result["uebersprungen"] == 4


def _wahl(item_id: str, item_type: str, expected: dict) -> AdaptiveItem:
    optionen = [{"id": o, "label": o.upper()} for o in "abcd"]
    return _item(item_id, item_type, expected, {"prompt": "p", "options": optionen})


def test_reshuffle_also_reshuffles_choice_options_and_keeps_answers(tmp_path, monkeypatch) -> None:
    factory = _factory(tmp_path)
    with factory() as s, s.begin():
        s.add_all([_wahl("c1", "single_choice", {"choice_id": "a"}),
                   _wahl("c2", "multiple_choice", {"choice_ids": ["a", "c"]})])
    rng = _Umkehr()
    monkeypatch.setattr(zufall_modul, "ZUFALL", rng)
    result = reihenfolgen_neu_mischen(factory)
    assert result["choice"] == 2
    for item_id, expected in (("c1", {"choice_id": "a"}), ("c2", {"choice_ids": ["a", "c"]})):
        item = _lesen(factory, item_id)
        assert item.expected_answer == expected
        assert [o["id"] for o in item.scoring_config["options"]] == ["d", "c", "b", "a"]
        assert item.scoring_config["prompt"] == "p"


def test_quiz_options_use_the_secure_rng_in_both_catalogs(monkeypatch) -> None:
    from spaces.learning.services.course_factory.practice_catalog import _shuffled_options
    from spaces.learning.services.course_factory.schemas import ActivityDraft, QuizOptionDraft
    from spaces.learning.services.werkstatt import katalog
    from spaces.learning.services.werkstatt.inhalt import Quiz

    monkeypatch.setattr(zufall_modul, "ZUFALL", _Umkehr())
    activity = ActivityDraft.model_construct(options=[QuizOptionDraft(option_id=o, text=o) for o in "abcd"])
    assert [o["id"] for o in _shuffled_options(activity)] == ["d", "c", "b", "a"]
    quiz = Quiz.model_validate({
        "kind": "quiz", "prompt": "Frage?", "concept_ids": ["k"],
        "options": [{"option_id": o, "text": o} for o in "abcd"], "correct_option_ids": ["a"],
    })
    _typ, _erwartet, scoring, _rubrik = katalog._item_felder("item-1", quiz)
    assert [o["id"] for o in scoring["options"]] == ["d", "c", "b", "a"]
