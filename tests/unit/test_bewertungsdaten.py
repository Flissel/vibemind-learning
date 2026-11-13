"""Bewertungsdaten: Musterloesung und Rubrik der aktuellen Uebungsaufgabe, nur fuer die Huelle (lesend)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService, AnswerCommand
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
    # Wie der Handschriftleser: das Leseformat und die Etiketten, die die Flaeche zeigt (A, B, ...).
    angezeigt = [o.label for o in turn.task.options]
    assert daten.antwortformat == {
        "roh": {"auswahl": "list with exactly one label"},
        "etiketten": {"optionen": {"A": angezeigt[0], "B": angezeigt[1]}, "links": {}, "rechts": {}},
    }
    # Keine Kennungen, nichts markiert die richtige Option.
    format_text = json.dumps(daten.antwortformat)
    assert "correct-1" not in format_text and "wrong-1" not in format_text
    engine.dispose()

    (tmp_path / "n").mkdir()
    engine, factory, course_id, _ = _store(tmp_path / "n", [_numeric()])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(service, course_id)
    daten = _daten(service, turn)
    assert daten.art == "deterministic"
    assert daten.musterloesung is None and daten.kriterien == []
    assert daten.antwortformat == {
        "roh": {"wert": "the number as written"},
        "etiketten": {"optionen": {}, "links": {}, "rechts": {}},
    }
    assert "4217" not in json.dumps(daten.model_dump(mode="json"))
    engine.dispose()


def test_bewertungsdaten_zuordnung_etiketten_ohne_loesung(tmp_path: Path) -> None:
    from spaces.learning.tests.unit.test_externe_bewertung import _zuordnung

    engine, factory, course_id, _ = _store(tmp_path, [_zuordnung()])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(service, course_id)
    daten = _daten(service, turn)
    rechts = [r["text"] for r in turn.task.presentation["right"]]
    assert daten.antwortformat == {
        "roh": {"paare": "left label -> right label"},
        "etiketten": {
            "optionen": {},
            "links": {"1": "Hund", "2": "Katze", "3": "Kuh"},
            "rechts": dict(zip("ABC", rechts)),
        },
    }
    format_text = json.dumps(daten.antwortformat)
    assert not any(k in format_text for k in ('"l1"', '"r1"', "pairs"))
    engine.dispose()


def test_bewertungsdaten_nur_fuer_typen_mit_vorschau(tmp_path: Path) -> None:
    # Was die Vorbewertung ablehnt (code), bekommt auch keine Bewertungsdaten: die Huelle fragt dann keine KI.
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("code")])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=_Rubrik())
    turn = _start(service, course_id)
    with pytest.raises(ValueError, match="no grading data"):
        _daten(service, turn)
    engine.dispose()

    (tmp_path / "p").mkdir()
    engine, factory, course_id, _ = _store(tmp_path / "p", [_rubric_item("penecho_canvas")])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=_Rubrik())
    turn = _start(service, course_id)
    assert _daten(service, turn).art == "rubric"
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


def _case_mit_vorlage() -> dict[str, object]:
    spec = _rubric_item("case")
    spec["scoring_config"] = {
        **spec["scoring_config"],  # type: ignore[dict-item]
        "presentation": {"fall": "Muster GmbH", "vorlage": "matrix"},
    }
    return spec


def test_bewertungsdaten_nennt_vorlage_als_vorgedruckt(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_case_mit_vorlage()])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=_Rubrik())
    turn = _start(service, course_id)

    daten = _daten(service, turn)

    prompt = daten.aufgabe["prompt"]
    assert prompt.startswith("Begruende den autorisierten Pfad.")
    assert "Fall: Muster GmbH" in prompt
    assert "vorgedruckt" in prompt
    # Der Browser sieht den Hinweis nicht.
    assert "vorgedruckt" not in turn.task.prompt
    engine.dispose()


def test_bewertungsdaten_ohne_fall_und_vorlage_unveraendert(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("case")])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=_Rubrik())
    turn = _start(service, course_id)

    daten = _daten(service, turn)

    assert daten.aufgabe["prompt"] == "Begruende den autorisierten Pfad."
    engine.dispose()


def test_rubrik_anfrage_bekommt_hinweis(tmp_path: Path) -> None:
    from spaces.learning.services.adaptive_engine.darstellung import bewertungshinweis

    engine, factory, course_id, _ = _store(tmp_path, [_case_mit_vorlage()])
    rubrik = _Rubrik()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=rubrik)
    turn = _start(service, course_id)
    hinweis = bewertungshinweis({"fall": "Muster GmbH", "vorlage": "matrix"})
    assert hinweis is not None

    attempt_id = _daten(service, turn).attempt_id
    service.answer(
        AnswerCommand(
            session_id=turn.session_id, actor_id=ACTOR,
            expected_session_revision=turn.session_revision,
            attempt_id=attempt_id, answer={"text": "Der Pfad."},
        )
    )

    request = rubrik.calls[0][0]
    assert request.expected_answer == "Der autorisierte Pfad.\n\n" + hinweis
    engine.dispose()
