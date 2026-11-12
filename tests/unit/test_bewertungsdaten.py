"""Bewertungsdaten: Musterloesung und Rubrik der aktuellen Uebungsaufgabe, nur fuer die Huelle (lesend)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.tests.unit.test_vorbewertung import (
    ACTOR,
    NOW,
    _Rubrik,
    _choice,
    _rubric_item,
    _start,
    _store,
)


def _numeric() -> dict[str, object]:
    return {
        "item_type": "numeric",
        "expected_answer": {"value": 4217.5},
        "scoring_config": {
            "prompt": "Wie gross ist der Wert?",
            "options": [],
            "source_refs": ["source://rag/n"],
            "remediation_tags": [],
            "presentation": {"unit": "kg"},
        },
        "rubric": [],
    }


def _daten(service, turn, **values):
    return service.bewertungsdaten(
        turn.session_id, values.pop("actor_id", ACTOR),
        values.pop("revision", turn.session_revision), values.pop("attempt_id", None),
    )


def test_bewertungsdaten_rubrik_liefert_musterloesung_und_kriterien(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("open")])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=_Rubrik())
    turn = _start(service, course_id)

    daten = _daten(service, turn)

    assert daten.art == "rubric"
    assert daten.item_type == "open"
    assert daten.musterloesung == "Der autorisierte Pfad."
    assert [(k.criterion_id, k.points) for k in daten.kriterien] == [("authority", 1)]
    assert daten.kriterien[0].description == "Nennt den Pfad."
    assert daten.antwortformat == {}
    assert daten.aufgabe["prompt"] == "Begruende den autorisierten Pfad."
    assert daten.attempt_id
    engine.dispose()


def test_bewertungsdaten_deterministisch_ohne_loesung(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(service, course_id)
    daten = _daten(service, turn)
    assert daten.art == "deterministic"
    assert daten.musterloesung is None
    assert daten.kriterien == []
    assert daten.antwortformat["form"] == "choice_id"
    assert {o["id"] for o in daten.antwortformat["optionen"]} == {"correct-1", "wrong-1"}
    assert {o["text"] for o in daten.antwortformat["optionen"]} == {
        "Quellen zuerst pruefen", "Antwort frei erfinden",
    }
    # Die Form nennt nur Felder, die der Lernende fuellt; nichts markiert die richtige Option.
    assert set(daten.antwortformat) == {"form", "optionen"}
    assert all(set(o) == {"id", "text"} for o in daten.antwortformat["optionen"])
    engine.dispose()

    (tmp_path / "n").mkdir()
    engine, factory, course_id, _ = _store(tmp_path / "n", [_numeric()])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(service, course_id)
    daten = _daten(service, turn)
    assert daten.art == "deterministic"
    assert daten.musterloesung is None and daten.kriterien == []
    assert daten.antwortformat == {"form": "value"}
    assert "4217" not in json.dumps(daten.model_dump(mode="json"))
    engine.dispose()


def test_bewertungsdaten_pruefung_verboten(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(
        service, course_id, mode="exam",
        item_count=2, item_types=["single_choice"], difficulty_bands=[],
    )
    with pytest.raises(PermissionError, match="exam"):
        _daten(service, turn)
    engine.dispose()


def test_bewertungsdaten_fremde_person(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(service, course_id)
    with pytest.raises(PermissionError, match="another actor"):
        _daten(service, turn, actor_id="fremd")
    engine.dispose()


def test_bewertungsdaten_veraltete_revision(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(service, course_id)
    with pytest.raises(PersistenceConflict, match="revision"):
        _daten(service, turn, revision=turn.session_revision + 1)
    engine.dispose()
