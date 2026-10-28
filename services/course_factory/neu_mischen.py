"""Reshuffle the shown order of stored ordering and matching items.

Catalogs projected before the secure shuffle carry orders that were a function of a public
seed (the item id), so the solution could be recomputed from the public code. This rewrites
only what is shown (``scoring_config`` options / right column); ``expected_answer`` and every
other scoring key stay as they are, because answers reference ids, not positions.

    python -m spaces.learning.services.course_factory.neu_mischen

Runs inside the learning-api container (database from its environment). Safe to run twice:
each run draws a fresh order that is never the solution order.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from spaces.learning.services import zufall
from spaces.learning.services.adaptive_engine.models import AdaptiveItem

SessionFactory = Callable[[], Session]


def _ordering(item: AdaptiveItem) -> dict[str, Any] | None:
    options = item.scoring_config.get("options")
    order = item.expected_answer.get("order")
    if not isinstance(options, list) or not isinstance(order, list) or len({o.get("id") for o in options}) < 2:
        return None
    by_id = {o["id"]: o for o in options}
    if sorted(by_id) != sorted(order):
        return None
    # Shuffle from the solution order, so the shown order is never the solution.
    return {**item.scoring_config, "options": zufall.anders_gemischt([by_id[i] for i in order])}


def _matching(item: AdaptiveItem) -> dict[str, Any] | None:
    presentation = item.scoring_config.get("presentation") or {}
    left, right = presentation.get("left"), presentation.get("right")
    pairs = item.expected_answer.get("pairs")
    if not isinstance(left, list) or not isinstance(right, list) or not isinstance(pairs, dict):
        return None
    if len({r.get("id") for r in right}) < 2:
        return None
    # The solution order of the right column: the partner of each left entry, top to bottom.
    partners = {r["id"]: r for r in right}
    try:
        aligned = [partners[pairs[entry["id"]]] for entry in left]
    except KeyError:
        return None
    if len(aligned) != len(right):
        return None
    shown = zufall.anders_gemischt(aligned)
    return {**item.scoring_config, "presentation": {**presentation, "right": shown}}


def reihenfolgen_neu_mischen(session_factory: SessionFactory) -> dict[str, int]:
    """Give every stored ordering/matching item a fresh, unpredictable shown order."""
    counts = {"ordering": 0, "matching": 0, "uebersprungen": 0}
    with session_factory() as session, session.begin():
        items = session.scalars(
            select(AdaptiveItem).where(AdaptiveItem.item_type.in_(("ordering", "matching")))
        ).all()
        for item in items:
            neu = _ordering(item) if item.item_type == "ordering" else _matching(item)
            if neu is None:
                counts["uebersprungen"] += 1
                continue
            item.scoring_config = neu  # a new dict: JSON columns do not track in-place edits
            counts[item.item_type] += 1
    return counts


def main() -> int:
    from spaces.learning.services.db.session import build_session_factory, create_learning_engine

    factory = build_session_factory(create_learning_engine(os.environ["LEARNING_DATABASE_URL"]))
    print(json.dumps(reihenfolgen_neu_mischen(factory), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
