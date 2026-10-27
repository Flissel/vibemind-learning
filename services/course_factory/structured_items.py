"""Hands-on items for the adaptive practice catalog, read off the lessons' structure.

The same numbered lists, command lines and term lists that become checkpoints in the
sequence player (``lernsequenz.aus_struktur``) become engine items here, in the forms
``evaluation.deterministic`` scores without a model: ``ordering``, ``matching`` and
``fill_in`` (a command with exactly one blank).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Literal

from spaces.learning.contracts.lernsequenz import Cloze, Matching, Ordering
from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.lernsequenz.aus_struktur import structured_checkpoints

ItemType = Literal["ordering", "matching", "fill_in"]


@dataclass(frozen=True)
class AdaptiveItemSpec:
    activity_id: str
    item_type: ItemType
    expected_answer: dict[str, Any]
    scoring_config: dict[str, Any]
    concept_ids: list[str]
    chapter_id: str


def _options(items, seed: str) -> list[dict[str, str]]:
    """Fixed per item, so the authored order (the solution) is not what the learner sees."""
    options = [{"id": item.item_id, "label": item.text} for item in items]
    random.Random(seed).shuffle(options)
    return options


def structured_items(draft: CourseDraft) -> list[AdaptiveItemSpec]:
    specs: list[AdaptiveItemSpec] = []
    for lesson in draft.lessons:
        for checkpoint in structured_checkpoints(lesson, after="lesson", refs=()):
            activity_id = f"{lesson.lesson_id}:{checkpoint.checkpoint_id}"
            if isinstance(checkpoint, Ordering):
                spec = AdaptiveItemSpec(
                    activity_id, "ordering",
                    {"order": list(checkpoint.correct)},
                    {"prompt": checkpoint.task, "options": _options(checkpoint.items, activity_id), "partial_credit": True},
                    list(lesson.concept_ids), lesson.chapter_id,
                )
            elif isinstance(checkpoint, Matching):
                spec = AdaptiveItemSpec(
                    activity_id, "matching",
                    {"pairs": dict(checkpoint.pairs)},
                    {
                        "prompt": checkpoint.task,
                        "left": [{"id": item.item_id, "label": item.text} for item in checkpoint.left],
                        "right": [{"id": item.item_id, "label": item.text} for item in checkpoint.right],
                        "partial_credit": True,
                    },
                    list(lesson.concept_ids), lesson.chapter_id,
                )
            elif isinstance(checkpoint, Cloze) and len(checkpoint.blanks) == 1:
                spec = AdaptiveItemSpec(
                    activity_id, "fill_in",
                    {"answers": list(checkpoint.blanks[0].accepted)},
                    {"prompt": checkpoint.task, "template": checkpoint.template, "shell": checkpoint.shell, "normalize": True},
                    list(lesson.concept_ids), lesson.chapter_id,
                )
            else:
                continue
            specs.append(spec)
    return specs
