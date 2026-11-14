"""Feldnamen der Vorlage je Aufgabe: was die Lernflaeche zeigt und was der Bewerter dazu erfaehrt."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from spaces.learning.services.adaptive_engine.darstellung import bewertungshinweis
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService

_MATRIX = ["Quick Win", "großes Projekt", "kleine Verbesserung", "Zeitfresser"]
_ONEPAGER = ["Ausgangslage", "Empfehlung", "Nutzen und Aufwand", "Risiken", "Nächster Schritt"]


def _zeige(item_type: str, presentation: dict[str, object]) -> dict[str, object]:
    item = SimpleNamespace(item_type=item_type, scoring_config={"presentation": presentation})
    return AdaptiveSessionService._presentation(item)


def test_gueltige_felder_erreichen_die_lernflaeche() -> None:
    assert _zeige("case", {"vorlage": "matrix", "vorlage_felder": _MATRIX}) == {
        "vorlage": "matrix", "vorlage_felder": _MATRIX}
    assert _zeige("open", {"vorlage": "onepager", "vorlage_felder": _ONEPAGER}) == {
        "vorlage": "onepager", "vorlage_felder": _ONEPAGER}


@pytest.mark.parametrize("item_type, presentation", [
    ("case", {"vorlage": "matrix", "vorlage_felder": _MATRIX[:3]}),
    ("case", {"vorlage": "onepager", "vorlage_felder": _MATRIX[:2]}),
    ("case", {"vorlage": "matrix", "vorlage_felder": "Quick Win"}),
    ("case", {"vorlage": "matrix", "vorlage_felder": ["a", "b", "c", 4]}),
    ("case", {"vorlage": "matrix", "vorlage_felder": ["a", "b", "c", "x" * 41]}),
    ("case", {"vorlage": "matrix", "vorlage_felder": ["a", "b", "", "d"]}),
    ("case", {"vorlage_felder": _MATRIX}),
    ("ordering", {"vorlage": "matrix", "vorlage_felder": _MATRIX}),
])
def test_ungueltige_felder_erreichen_die_lernflaeche_nicht(item_type: str, presentation: dict[str, object]) -> None:
    assert "vorlage_felder" not in _zeige(item_type, presentation)


def test_bewerterhinweis_nennt_die_eigenen_felder_mit_ihrer_lage() -> None:
    hinweis = bewertungshinweis({"vorlage": "matrix", "vorlage_felder": _MATRIX})
    assert hinweis is not None
    assert "Quick Win (viel Nutzen, wenig Aufwand)" in hinweis
    assert "Zeitfresser (wenig Nutzen, viel Aufwand)" in hinweis
    assert "Große Vorhaben" not in hinweis


def test_bewerterhinweis_nennt_die_eigenen_onepager_abschnitte() -> None:
    hinweis = bewertungshinweis({"vorlage": "onepager", "vorlage_felder": _ONEPAGER})
    assert hinweis is not None
    assert "Ausgangslage, Empfehlung, Nutzen und Aufwand, Risiken, Nächster Schritt" in hinweis
    assert "Empfohlene Use Cases" not in hinweis


def test_bewerterhinweis_mit_ungueltigen_feldern_nennt_die_standardnamen() -> None:
    hinweis = bewertungshinweis({"vorlage": "matrix", "vorlage_felder": _MATRIX[:2]})
    assert hinweis is not None and "Quick Wins (viel Nutzen, wenig Aufwand)" in hinweis
