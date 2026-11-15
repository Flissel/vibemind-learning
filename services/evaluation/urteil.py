"""The one rule that turns a score into a verdict.

Rubric-graded answers (open, case, code, drawing; also a person's review of one) are "correct"
from 0.7: a good answer with a small wish is not downgraded. Deterministic answers (choice,
ordering, matching, cloze, number) are correct only when complete: five of six pairs stay partial.
"""

from __future__ import annotations

from typing import Literal

Urteil = Literal["correct", "partial", "incorrect"]

RICHTIG_AB_RUBRIK = 0.7
TEILWEISE_AB_RUBRIK = 0.4
RICHTIG_AB_FEST = 0.999
TEILWEISE_AB_FEST = 0.65


def urteil(score: float, *, rubrik: bool) -> Urteil:
    richtig, teilweise = (
        (RICHTIG_AB_RUBRIK, TEILWEISE_AB_RUBRIK) if rubrik else (RICHTIG_AB_FEST, TEILWEISE_AB_FEST)
    )
    return "correct" if score >= richtig else "partial" if score >= teilweise else "incorrect"
