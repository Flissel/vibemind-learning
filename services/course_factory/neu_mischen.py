"""Reshuffle the shown order of stored ordering, matching and choice items, and of stored
Lernsequenz matching checkpoints.

Catalogs and sequences made before the secure shuffle carry orders that were a function of a
public seed (item id, checkpoint id), so the solution could be recomputed from the public
code. This rewrites only what is shown (``scoring_config`` options / right column);
``expected_answer``, ``pairs`` and every other key stay as they are, because answers
reference ids, not positions.

    python -m spaces.learning.services.course_factory.neu_mischen                  # catalog items
    python -m spaces.learning.services.course_factory.neu_mischen --lernsequenzen  # stored sequences

Runs inside the learning-api container (database / LEARNING_ARTIFACT_ROOT from its environment).
Safe to run twice: each run draws a fresh order.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from spaces.learning.services import zufall
from spaces.learning.services.adaptive_engine.models import AdaptiveItem
from spaces.learning.services.lernsequenz.store import SequenceStore

SessionFactory = Callable[[], Session]


def _ordering(item: AdaptiveItem) -> dict[str, Any] | None:
    options = item.scoring_config.get("options")
    order = item.expected_answer.get("order")
    if not isinstance(options, list) or not isinstance(order, list) or len(options) < 2:
        return None
    by_id = {o["id"]: o for o in options}
    if len(by_id) != len(options) or sorted(by_id) != sorted(order):
        return None
    # Shuffle from the solution order, so the shown order is never the solution (3+ entries).
    return {**item.scoring_config, "options": zufall.anders_gemischt([by_id[i] for i in order])}


def _aligned(left_ids: list[Any], right_ids: list[Any], pairs: dict[Any, Any]) -> list[Any] | None:
    """The right ids in solution order (partner of each left entry), or None unless the pairs
    are a bijection: otherwise one right entry would be duplicated and another dropped."""
    aligned = [pairs[i] for i in left_ids]
    if len(set(right_ids)) != len(right_ids) or sorted(aligned) != sorted(right_ids):
        return None
    return aligned


def _matching(item: AdaptiveItem) -> dict[str, Any] | None:
    presentation = item.scoring_config.get("presentation") or {}
    left, right = presentation.get("left"), presentation.get("right")
    pairs = item.expected_answer.get("pairs")
    if not isinstance(left, list) or not isinstance(right, list) or not isinstance(pairs, dict) or len(right) < 2:
        return None
    by_id = {r["id"]: r for r in right}
    aligned = _aligned([entry["id"] for entry in left], [r["id"] for r in right], pairs)
    if aligned is None:
        return None
    shown = zufall.anders_gemischt([by_id[i] for i in aligned])
    return {**item.scoring_config, "presentation": {**presentation, "right": shown}}


def _choice(item: AdaptiveItem) -> dict[str, Any] | None:
    options = item.scoring_config.get("options")
    if not isinstance(options, list) or len(options) < 2:
        return None
    ids = [o["id"] for o in options]
    if len(set(ids)) != len(ids):
        return None
    return {**item.scoring_config, "options": zufall.gemischt(options)}


_ARTEN = {
    "ordering": ("ordering", _ordering),
    "matching": ("matching", _matching),
    "single_choice": ("choice", _choice),
    "multiple_choice": ("choice", _choice),
}


def reihenfolgen_neu_mischen(session_factory: SessionFactory) -> dict[str, int]:
    """Give every stored ordering / matching / choice item a fresh, unpredictable shown order."""
    counts = {"ordering": 0, "matching": 0, "choice": 0, "uebersprungen": 0}
    with session_factory() as session, session.begin():
        items = session.scalars(select(AdaptiveItem).where(AdaptiveItem.item_type.in_(tuple(_ARTEN)))).all()
        for item in items:
            art, mischen = _ARTEN[item.item_type]
            try:
                neu = mischen(item)
            except (KeyError, AttributeError, TypeError):
                neu = None
            if neu is None:
                counts["uebersprungen"] += 1
                continue
            item.scoring_config = neu  # a new dict: JSON columns do not track in-place edits
            counts[art] += 1
    return counts


def lernsequenzen_neu_mischen(store_or_root: SequenceStore | Path) -> dict[str, int]:
    """Reshuffle the right column of every stored matching checkpoint (``pairs`` and ids stay)."""
    store = store_or_root if isinstance(store_or_root, SequenceStore) else SequenceStore(Path(store_or_root))
    counts = {"sequenzen": 0, "matching": 0, "uebersprungen": 0}
    for sequence_id in store.sequence_ids():
        try:
            sequence = store.load(sequence_id)
            checkpoints = []
            geaendert = 0
            for checkpoint in sequence.checkpoints:
                if checkpoint.kind == "matching":
                    by_id = {r.item_id: r for r in checkpoint.right}
                    aligned = _aligned(
                        [e.item_id for e in checkpoint.left], [r.item_id for r in checkpoint.right], checkpoint.pairs,
                    )
                    if aligned is not None:
                        shown = zufall.anders_gemischt([by_id[i] for i in aligned], lambda e: e.item_id)
                        checkpoint = checkpoint.model_copy(update={"right": tuple(shown)})
                        geaendert += 1
                checkpoints.append(checkpoint)
            if geaendert:
                store.save(sequence.model_copy(update={"checkpoints": tuple(checkpoints)}))
        except (LookupError, ValueError, KeyError, OSError):
            counts["uebersprungen"] += 1
            continue
        if geaendert:
            counts["sequenzen"] += 1
            counts["matching"] += geaendert
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neu_mischen")
    parser.add_argument("--lernsequenzen", action="store_true", help="stored sequences instead of catalog items")
    args = parser.parse_args(argv)
    if args.lernsequenzen:
        result = lernsequenzen_neu_mischen(Path(os.environ["LEARNING_ARTIFACT_ROOT"]))
    else:
        from spaces.learning.services.db.session import build_session_factory, create_learning_engine

        factory = build_session_factory(create_learning_engine(os.environ["LEARNING_DATABASE_URL"]))
        result = reihenfolgen_neu_mischen(factory)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
