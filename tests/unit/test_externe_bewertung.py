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


def _gelesen(roh: dict[str, object] | None, *, lesbar: bool = True, text: str = "A") -> dict[str, object]:
    # Dieselbe Form wie die Lesung des Handschriftlesers (GelesenV1): Etiketten, wie geschrieben.
    return {"grader": QUELLE, "gelesen": {"lesbar": lesbar, "text": text, "roh": roh or {}}}


_BUCHSTABEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _etikett(turn, praefix: str) -> str:
    """Das Etikett (A, B, ...), unter dem die Flaeche die Option zeigt (Reihenfolge wie angezeigt)."""
    index = next(i for i, o in enumerate(turn.task.options) if o.id.startswith(praefix))
    return _BUCHSTABEN[index]


def _zahl(wert: float) -> dict[str, object]:
    return {
        "item_type": "numeric",
        "expected_answer": {"value": wert},
        "scoring_config": {
            "prompt": "Wie gross ist x?",
            "options": [],
            "source_refs": ["source://rag/n"],
            "remediation_tags": [],
        },
        "rubric": [],
    }


def _zuordnung() -> dict[str, object]:
    return {
        "item_type": "matching",
        "expected_answer": {"pairs": {"l1": "r1", "l2": "r2", "l3": "r3"}},
        "scoring_config": {
            "prompt": "Ordnen Sie zu.",
            "options": [],
            "source_refs": ["source://rag/m"],
            "remediation_tags": [],
            "presentation": {
                "left": [
                    {"id": "l1", "text": "Hund"},
                    {"id": "l2", "text": "Katze"},
                    {"id": "l3", "text": "Kuh"},
                ],
                "right": [
                    {"id": "r1", "text": "bellt"},
                    {"id": "r2", "text": "miaut"},
                    {"id": "r3", "text": "muht"},
                ],
            },
        },
        "rubric": [],
    }


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
        _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen({"auswahl": ["A"]}))
    assert _anzahl(factory) == 0
    engine.dispose()


@pytest.mark.parametrize("richtig", [True, False])
def test_extern_deterministisch_learning_vergleicht_selbst(tmp_path: Path, richtig: bool) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    # Die KI liest, was auf der Flaeche steht: das Etikett der Option, nicht ihre Kennung.
    etikett = _etikett(turn, "correct-" if richtig else "wrong-")

    vor = _vor(
        service, turn, handschrift_png=PNG,
        externe_bewertung=_gelesen({"auswahl": [etikett]}, text=etikett),
    )

    assert vor.bewertet_von == "ki_flaeche"
    assert vor.lesbar is True and vor.art == "deterministic"
    assert vor.score == (1.0 if richtig else 0.0)
    assert vor.gelesen_text == etikett
    assert vor.feedback is not None
    assert vor.feedback.state == ("correct" if richtig else "incorrect")
    assert leser.calls == []  # der Handschriftleser wird nicht aufgerufen
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        assert row is not None and row.evaluator_versions["grader"] == "ki_flaeche"
        assert row.gelesen["roh"] == {"auswahl": [etikett]}
    engine.dispose()


def test_extern_einfachauswahl_etikett_b(tmp_path: Path) -> None:
    # Die richtige Option steht als B auf der Flaeche (Reihenfolge je Sitzung): "B" ist richtig.
    for versuch in range(20):
        ordner = tmp_path / str(versuch)
        ordner.mkdir()
        engine, factory, course_id, _ = _store(ordner, [_choice(1)])
        service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=_Leser())
        turn = _start(service, course_id)
        if _etikett(turn, "correct-") == "B":
            break
        engine.dispose()
    else:
        pytest.fail("keine Sitzung zeigt die richtige Option als B")

    vor = _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen({"auswahl": ["B"]}, text="B"))

    assert vor.score == 1.0 and vor.feedback is not None and vor.feedback.state == "correct"
    engine.dispose()


def test_extern_zahl_mit_deutschem_komma(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_zahl(1.5)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=_Leser())
    turn = _start(service, course_id)

    vor = _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen({"wert": "1,5"}, text="x = 1,5"))

    assert vor.bewertet_von == "ki_flaeche"
    assert vor.score == 1.0 and vor.feedback is not None and vor.feedback.state == "correct"
    assert vor.gelesen_text == "x = 1,5"
    engine.dispose()


def test_extern_zuordnung_mit_etikettenpaaren(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_zuordnung()])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=_Leser())
    turn = _start(service, course_id)
    rechts = [r["id"] for r in turn.task.presentation["right"]]
    # Links 1, 2, 3 (Hund, Katze, Kuh), rechts A, B, C in der angezeigten Reihenfolge.
    paare = {str(n): _BUCHSTABEN[rechts.index(f"r{n}")] for n in (1, 2, 3)}

    vor = _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen({"paare": paare}, text="1-B"))

    assert vor.score == 1.0 and vor.feedback is not None and vor.feedback.state == "correct"
    engine.dispose()


@pytest.mark.parametrize(
    "roh",
    [
        {"auswahl": ["Z"]},  # kein Etikett der Aufgabe
        {"auswahl": ["correct-1"]},  # Kennungen sind keine Etiketten
        {"auswahl": ["A", "B"]},  # Einfachauswahl mit zwei Etiketten
        {"wert": "1,5"},  # Feld einer anderen Aufgabenart
        {},
    ],
    ids=["fremdes_etikett", "kennung", "zwei_etiketten", "falsches_feld", "leer"],
)
def test_extern_nicht_zuordenbare_lesung_wird_abgelehnt(tmp_path: Path, roh) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)

    # Die Huelle faellt dann auf die Bewertung durch den Server zurueck.
    with pytest.raises(ValueError):
        _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen(roh))
    assert _anzahl(factory) == 0
    assert leser.calls == []
    engine.dispose()


def test_extern_mehrdeutige_zahl_wird_abgelehnt(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_zahl(1.5)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=_Leser())
    turn = _start(service, course_id)

    with pytest.raises(ValueError):
        _vor(service, turn, handschrift_png=PNG, externe_bewertung=_gelesen({"wert": "1.500"}))
    assert _anzahl(factory) == 0
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
        # Die fruehere Form mit fertiger Antwort (Kennungen) gibt es nicht mehr.
        {"grader": QUELLE, "gelesen": {"lesbar": True, "text": "A", "antwort": {"choice_id": "a"}}},
        {"grader": QUELLE, "gelesen": {"lesbar": True, "text": "x" * 201, "roh": {"auswahl": ["A"]}}},
    ],
    ids=["falscher_grader", "beide_formen", "keine_form", "unbekanntes_feld", "alte_form", "text_zu_lang"],
)
def test_extern_form_wird_streng_geprueft(wert) -> None:
    with pytest.raises(ValidationError):
        VorbewertungCommand(
            session_id=str(uuid4()), actor_id=ACTOR, expected_session_revision=1,
            handschrift_png=PNG, externe_bewertung=wert,
        )
