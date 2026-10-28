"""Shown order of ordering and matching tasks: unpredictable, and never the solution.

This code is public. A permutation recomputable from a seed anyone can know (item id,
checkpoint id) would hand out the answer, so shown orders come from the operating system's
randomness. Answers reference ids, never positions, so the shown order is free to change.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# Looked up at call time (tests replace it with a scripted stand-in).
ZUFALL = secrets.SystemRandom()


def _id(eintrag: Any) -> Any:
    return eintrag["id"]


def anders_gemischt(
    eintraege: list[T], schluessel: Callable[[T], Any] = _id, zufall: Any = None,
) -> list[T]:
    """A shuffled copy of ``eintraege`` (given in solution order).

    With three or more distinct entries (by ``schluessel``) the result differs from the
    solution order. With exactly two it is a plain uniform shuffle: "never the solution" would
    leave one possible order, i.e. name the answer. With fewer, any order."""
    rng = zufall if zufall is not None else ZUFALL
    gemischt = list(eintraege)
    if len({schluessel(e) for e in eintraege}) < 3:
        rng.shuffle(gemischt)
        return gemischt
    vorgabe = [schluessel(e) for e in eintraege]
    while True:
        rng.shuffle(gemischt)
        if [schluessel(e) for e in gemischt] != vorgabe:
            return gemischt


def gemischt(eintraege: list[T], zufall: Any = None) -> list[T]:
    """A uniformly shuffled copy, for choice options: their order carries no solution order."""
    rng = zufall if zufall is not None else ZUFALL
    kopie = list(eintraege)
    rng.shuffle(kopie)
    return kopie
