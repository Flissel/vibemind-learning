"""Shown order of practice items is decided when the task is served, not by the stored row.

Old catalog rows carry orders recomputable from public ids, and the table is append-only, so
they cannot be rewritten. Serving sorts by a keyed hash (secret per process, salted with the
session and the item): stable for one session, unpredictable without the key.
"""

from __future__ import annotations

from types import SimpleNamespace

from spaces.learning.services import zufall
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService

IDS = [f"s{i}" for i in range(1, 6)]


def _item(item_type: str, *, options=(), presentation=None, expected=None, item_id: str = "item-1"):
    return SimpleNamespace(
        id=item_id, item_type=item_type, difficulty=0.5, expected_answer=expected or {},
        scoring_config={
            "prompt": "Aufgabe?", "source_refs": [], "options": [{"id": i, "label": i.upper()} for i in options],
            "presentation": presentation or {},
        },
    )


def _served(item, session_id: str = "sess-1") -> list[str]:
    return [o.id for o in AdaptiveSessionService._public_task(item, session_id).options]


def test_an_ordering_stored_in_solution_order_is_never_served_in_it() -> None:
    for n in range(200):
        item = _item("ordering", options=IDS, expected={"order": IDS}, item_id=f"item-{n}")
        served = _served(item)
        assert sorted(served) == sorted(IDS) and served != IDS


def test_the_order_is_stable_within_a_session_and_differs_between_sessions() -> None:
    item = _item("ordering", options=IDS, expected={"order": IDS})
    assert _served(item, "sess-1") == _served(item, "sess-1")
    assert len({tuple(_served(item, f"sess-{n}")) for n in range(30)}) > 1


def test_the_order_depends_on_the_process_key_not_only_on_public_ids(monkeypatch) -> None:
    item = _item("single_choice", options=list("abcdef"))
    orders = set()
    for n in range(10):
        monkeypatch.setattr(zufall, "SCHLUESSEL", bytes([n]) * 32)
        orders.add(tuple(_served(item)))
    assert len(orders) > 1


def test_matching_right_column_is_never_aligned_with_the_left() -> None:
    left = [{"id": f"b{i}", "label": f"L{i}"} for i in range(1, 5)]
    right = [{"id": f"e{i}", "label": f"R{i}"} for i in range(1, 5)]
    pairs = {f"b{i}": f"e{i}" for i in range(1, 5)}
    for n in range(200):
        item = _item("matching", presentation={"left": left, "right": right}, expected={"pairs": pairs},
                     item_id=f"item-{n}")
        task = AdaptiveSessionService._public_task(item, "sess-1")
        shown_right = [r["id"] for r in task.presentation["right"]]
        assert task.presentation["left"] == left
        assert sorted(shown_right) == sorted(pairs.values())
        assert shown_right != [pairs[entry["id"]] for entry in left]


def test_orders_other_than_the_solution_are_equally_likely() -> None:
    # Security review: rotating a solution-order draw made "solution shifted by one" twice as
    # likely, so the most frequent order across many sessions named the answer.
    from collections import Counter

    ids = ["s1", "s2", "s3"]
    counts = Counter(
        tuple(zufall.verdeckt_sortiert(ids, f"sess-{n}:item", lambda e: e, ids)) for n in range(3000)
    )
    assert tuple(ids) not in counts and len(counts) == 5
    assert max(counts.values()) < 1.3 * min(counts.values())
