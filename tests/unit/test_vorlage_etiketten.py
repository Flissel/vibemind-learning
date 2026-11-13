"""Schichtuebergreifend: jede Etikett der gedruckten Vorlage kennt auch der Bewerterhinweis.

Die Huelle importiert den Learning-Dienst nicht (und umgekehrt); dieser Test darf beides und haelt die
Beschriftungen beider Seiten gleich.
"""

from __future__ import annotations

import re

import pytest

from spaces.learning.huelle.vorlage import VORLAGEN, vorlagenplan
from spaces.learning.services.adaptive_engine.darstellung import bewertungshinweis

_FELD = {"x": 0, "y": 0, "w": 1480, "h": 1240}


def _etiketten(art: str) -> list[str]:
    plan = vorlagenplan(art, _FELD)
    assert plan is not None
    # "1. Ausgangslage" -> "Ausgangslage"; "Nutzen ↑" / "Aufwand →" -> Achsenname ohne Pfeil.
    return [re.sub(r"^\d+\.\s+", "", re.sub(r"\s*[↑→]\s*$", "", text)) for text, _ in plan.texte]


@pytest.mark.parametrize("art", VORLAGEN)
def test_jedes_vorlagen_etikett_steht_im_bewerterhinweis(art: str) -> None:
    etiketten = _etiketten(art)
    assert len(etiketten) == 6
    hinweis = bewertungshinweis({"vorlage": art})
    assert hinweis is not None
    fehlt = [e for e in etiketten if e not in hinweis]
    assert not fehlt, fehlt
