"""Externe Bewertung: Learning nimmt die Note der KI-Flaeche an, prueft sie und vermerkt den Bewerter."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from spaces.learning.services.adaptive_engine.models import LearningVorbewertung
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    VorbewertungCommand,
)
from spaces.learning.tests.unit.test_vorbewertung import (
    ACTOR,
    NOW,
    PNG,
    _choice,
    _Leser,
    _richtig,
    _rubric_item,
    _Rubrik,
    _start,
    _store,
    _vor,
)

QUELLE = "ki_flaeche"


def _rubrik_extern(punkte: float = 1.0, **ueberschreiben: object) -> dict[str, object]:
    wert: dict[str, object] = {
        "grader": QUELLE,
        "kriterien": [
            {"criterion_id": "authority", "punkte": punkte, "rueckmeldung": "Nennt den Pfad."}
        ],
        "rueckmeldung": "Gut begruendet.",
    }
    wert.update(ueberschreiben)
    return wert


def _gelesen(antwort: dict[str, object] | None, *, lesbar: bool = True) -> dict[str, object]:
    return {"grader": QUELLE, "gelesen": {"lesbar": lesbar, "text": "A", "antwort": antwort}}


def _anzahl(factory) -> int:
    with factory() as db:
        return db.scalar(select(func.count()).select_from(LearningVorbewertung)) or 0


def test_extern_rubrik_wird_uebernommen(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("open")])
    rubrik = _Rubrik()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=rubrik)
    turn = _start(service, course_id)

    vor = _vor(service, turn, handschrift_png=PNG, externe_bewertung=_rubrik_extern(1.0))

    assert vor.bewertet_von == "ki_flaeche"
    assert vor.art == "rubric" and vor.lesbar is True
    assert vor.score == 1.0 and vor.accepted is True
    assert vor.feedback is not None and vor.feedback.state == "correct"
    assert vor.feedback.criteria[0].feedback == "Nennt den Pfad."
    assert vor.feedback.criteria[0].score == 1.0
    assert rubrik.calls == []  # der Rubrik-Evaluator des Servers wird nicht gefragt
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        assert row is not None
        assert row.evaluator_versions["grader"] == "ki_flaeche"
        assert row.evaluator_type == "rubric"
    engine.dispose()


def test_extern_rubrik_teilpunkte_score_ist_summe_durch_maximum(tmp_path: Path) -> None:
    spec = _rubric_item("open")
    spec["rubric"] = [
        {"criterion_id": "a", "description": "A.", "points": 2},
        {"criterion_id": "b", "description": "B.", "points": 2},
    ]
    engine, factory, course_id, _ = _store(tmp_path, [spec])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=_Rubrik())
    turn = _start(service, course_id)
    extern = _rubrik_extern(
        kriterien=[
            {"criterion_id": "a", "punkte": 2, "rueckmeldung": "ok"},
            {"criterion_id": "b", "punkte": 1, "rueckmeldung": "teils"},
        ]
    )

    vor = _vor(service, turn, handschrift_png=PNG, externe_bewertung=extern)

    assert vor.score == pytest.approx(0.75)
    assert vor.feedback is not None and vor.feedback.state == "partial"
    engine.dispose()


@pytest.mark.parametrize(
    "kriterien",
    [
        [{"criterion_id": "authority", "punkte": 1.5, "rueckmeldung": "zu viel"}],
        [
            {"criterion_id": "authority", "punkte": 1, "rueckmeldung": "ok"},
            {"criterion_id": "erfunden", "punkte": 0, "rueckmeldung": "x"},
        ],
        [{"criterion_id": "erfunden", "punkte": 1, "rueckmeldung": "x"}],
        [],
        [
            {"criterion_id": "authority", "punkte": 1, "rueckmeldung": "ok"},
            {"criterion_id": "authority", "punkte": 1, "rueckmeldung": "doppelt"},
        ],
        [{"criterion_id": "authority", "punkte": -1, "rueckmeldung": "negativ"}],
    ],
    ids=["ueber_maximum", "unbekannt_dazu", "unbekannt_statt", "fehlt", "doppelt", "negativ"],
)
def test_extern_rubrik_ungueltig_wird_abgelehnt(tmp_path: Path, kriterien) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("open")])
    rubrik = _Rubrik()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=rubrik)
    turn = _start(service, course_id)

    with pytest.raises(ValueError):
        _vor(
            service, turn, handschrift_png=PNG,
            externe_bewertung=_rubrik_extern(kriterien=kriterien),
        )

    assert _anzahl(factory) == 0
    assert rubrik.calls == []
    engine.dispose()


def test_extern_rubrik_form_auf_fester_loesung_wird_abgelehnt(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=_Leser())
    turn = _start(service, course_id)

    with pytest.raises(ValueError):
        _vor(service, turn, handschrift_png=PNG, externe_bewertung=_rubrik_extern())
    assert _anzahl(factory) == 0
    engine.dispose()


def test_extern_gelesen_form_auf_rubrik_wird_abgelehnt(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("open")])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=_Rubrik())
    turn = _start(service, course_id)

    with pytest.raises(ValueError):
        _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen({"choice_id": "x"}))
    assert _anzahl(factory) == 0
    engine.dispose()


@pytest.mark.parametrize("richtig", [True, False])
def test_extern_deterministisch_learning_vergleicht_selbst(tmp_path: Path, richtig: bool) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    antwort = _richtig(turn) if richtig else {"choice_id": "wrong-1"}

    vor = _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen(antwort))

    assert vor.bewertet_von == "ki_flaeche"
    assert vor.lesbar is True and vor.art == "deterministic"
    assert vor.score == (1.0 if richtig else 0.0)
    assert vor.gelesen_text == "A"
    assert vor.feedback is not None
    assert vor.feedback.state == ("correct" if richtig else "incorrect")
    assert leser.calls == []  # der Handschriftleser wird nicht aufgerufen
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        assert row is not None and row.evaluator_versions["grader"] == "ki_flaeche"
    engine.dispose()


def test_extern_deterministisch_unlesbar(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)

    vor = _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen(None, lesbar=False))

    assert vor.lesbar is False
    assert vor.score is None and vor.accepted is None and vor.feedback is None
    assert vor.bewertet_von == "ki_flaeche"
    assert leser.calls == []
    engine.dispose()


def test_ohne_extern_bewertet_server_und_vermerkt_es(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=_Leser())
    turn = _start(service, course_id)

    vor = _vor(service, turn, answer=_richtig(turn))

    assert vor.bewertet_von == "server"
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        assert row is not None and row.evaluator_versions["grader"] == "server"
    engine.dispose()


def test_extern_nur_mit_bild() -> None:
    basis = dict(session_id=str(uuid4()), actor_id=ACTOR, expected_session_revision=1)
    with pytest.raises(ValidationError):
        VorbewertungCommand(**basis, answer={"choice_id": "a"}, externe_bewertung=_gelesen({}))
    with pytest.raises(ValidationError):
        VorbewertungCommand(
            **basis, handschrift_png=PNG, korrigiert_aus=str(uuid4()),
            externe_bewertung=_gelesen({}),
        )


@pytest.mark.parametrize(
    "wert",
    [
        {**_rubrik_extern(), "grader": "server"},
        {**_rubrik_extern(), **_gelesen({})},  # beide Formen
        {"grader": QUELLE},  # keine Form
        {**_rubrik_extern(), "extra": 1},
    ],
    ids=["falscher_grader", "beide_formen", "keine_form", "unbekanntes_feld"],
)
def test_extern_form_wird_streng_geprueft(wert) -> None:
    with pytest.raises(ValidationError):
        VorbewertungCommand(
            session_id=str(uuid4()), actor_id=ACTOR, expected_session_revision=1,
            handschrift_png=PNG, externe_bewertung=wert,
        )
