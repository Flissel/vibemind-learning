"""Schichtuebergreifend: jede Etikett der gedruckten Vorlage kennt auch der Bewerterhinweis.

Die Huelle importiert den Learning-Dienst nicht (und umgekehrt); dieser Test darf beides und haelt die
Beschriftungen und Grenzen beider Seiten gleich.
"""

from __future__ import annotations

import re

import pytest

from spaces.learning.huelle import vorlage as huelle_vorlage
from spaces.learning.huelle.vorlage import VORLAGEN, vorlagenplan
from spaces.learning.services.adaptive_engine import darstellung
from spaces.learning.services.adaptive_engine.darstellung import bewertungshinweis

_FELD = {"x": 0, "y": 0, "w": 1480, "h": 1240}
_EIGENE = {
    "matrix": ["Quick Win", "großes Projekt", "kleine Verbesserung", "Zeitfresser"],
    "onepager": ["Ausgangslage", "Empfehlung", "Nutzen und Aufwand", "Risiken", "Nächster Schritt"],
}


def _etiketten(art: str, felder: list[str] | None = None) -> list[str]:
    plan = vorlagenplan(art, _FELD, felder=felder)
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


@pytest.mark.parametrize("art", VORLAGEN)
def test_eigene_feldnamen_stehen_gedruckt_und_im_bewerterhinweis(art: str) -> None:
    etiketten = _etiketten(art, _EIGENE[art])
    assert all(f in etiketten for f in _EIGENE[art])
    hinweis = bewertungshinweis({"vorlage": art, "vorlage_felder": _EIGENE[art]})
    assert hinweis is not None
    fehlt = [e for e in etiketten if e not in hinweis]
    assert not fehlt, fehlt


def test_standardnamen_und_grenzen_sind_auf_beiden_seiten_gleich() -> None:
    assert huelle_vorlage.STANDARD_FELDER == darstellung.STANDARD_FELDER
    assert huelle_vorlage.FELDER_ANZAHL == darstellung.FELDER_ANZAHL
    assert huelle_vorlage.FELD_MAX == darstellung.FELD_MAX
