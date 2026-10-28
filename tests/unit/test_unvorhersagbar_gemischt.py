"""Shown order of ordering and matching tasks: unpredictable and never the solution.

The code is public, so a permutation recomputable from a seed (item id, checkpoint id) would
hand out the answer. Ordering options and matching right columns come from a secure RNG.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from spaces.learning.services import zufall as zufall_modul
from spaces.learning.services.course_factory.structured_items import structured_items
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
