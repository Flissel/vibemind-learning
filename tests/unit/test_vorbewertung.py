"""Vorbewertung: a preview grading that changes nothing until the learner confirms it."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ConceptMastery,
    ItemConcept,
    LearningAttempt,
    LearningEvaluation,
    LearningResponse,
    LearningReviewItem,
    LearningVorbewertung,
    MasteryEvidence,
    Misconception,
    ReviewSchedule,
)
from spaces.learning.services.adaptive_engine import session_service as session_modul
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    AnswerCommand,
    SessionStartCommand,
    VorbewertungCommand,
)
from spaces.learning.services.db.models import Base
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.services.evaluation.handschrift import GelesenV1
from spaces.learning.services.evaluation.schemas import (
    CriterionEvaluation,
    RubricDecision,
)

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
ACTOR = "lernende-1"
PNG = b"\x89PNG\r\n\x1a\nhandschrift-test"


# ---- Aufbau ---------------------------------------------------------------------------


def _choice(index: int) -> dict[str, object]:
    return {
        "item_type": "single_choice",
        "expected_answer": {"choice_id": f"correct-{index}"},
        "scoring_config": {
            "prompt": f"Welche Aussage gilt auf Stufe {index}?",
            "options": [
                {"id": f"correct-{index}", "label": "Quellen zuerst pruefen"},
                {"id": f"wrong-{index}", "label": "Antwort frei erfinden"},
            ],
            "source_refs": [f"source://rag/{index}"],
            "remediation_tags": ["authority_bypass"],
        },
        "rubric": [],
    }


def _rubric_item(item_type: str) -> dict[str, object]:
    return {
        "item_type": item_type,
        "expected_answer": {"text": "Der autorisierte Pfad."},
        "scoring_config": {
            "prompt": "Begruende den autorisierten Pfad.",
            "options": [],
            "source_refs": ["source://authority/1"],
            "remediation_tags": [],
            "model_version": "stub-v1",
            "source_version": "course-1",
        },
        "rubric": [
            {"criterion_id": "authority", "description": "Nennt den Pfad.", "points": 1}
        ],
    }


def _store(tmp_path: Path, specs: list[dict[str, object]]):
    engine = create_engine(f"sqlite:///{tmp_path / 'vorbewertung.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, connection_record) -> None:
        del connection_record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    course_id = str(uuid4())
    concept_id = str(uuid4())
    with factory() as db, db.begin():
        db.add(
            AdaptiveConcept(
                id=concept_id, course_id=course_id, course_revision=1,
                concept_key="rag.authority", title="Quellenautoritaet",
            )
        )
        for index, spec in enumerate(specs, start=1):
            item_id = str(uuid4())
            db.add(
                AdaptiveItem(
                    id=item_id, course_id=course_id, course_revision=1,
                    activity_id=f"activity-{index}", difficulty=index / (len(specs) + 1),
                    quality_status="approved", **spec,
                )
            )
            db.add(
                ItemConcept(
                    item_id=item_id, concept_id=concept_id,
                    course_id=course_id, course_revision=1,
                )
            )
    return engine, factory, course_id, concept_id


class _Leser:
    """Fake handwriting reader: answers from a queue and counts its calls."""

    def __init__(self, *antworten: dict[str, object] | None) -> None:
        self._antworten = list(antworten)
        self.calls: list[tuple[dict[str, object], bytes]] = []

    def lesen(self, task, png):
        self.calls.append((task, png))
        antwort = self._antworten.pop(0)
        if antwort is None:
            return GelesenV1(lesbar=False, roh={}, text=""), None
        return GelesenV1(lesbar=True, roh={"auswahl": ["A"]}, text="A"), antwort


class _Rubrik:
    """Fake rubric boundary with the image keyword; counts its calls."""

    def __init__(self, *, score: float = 0.8, accepted: bool = True, codes=None) -> None:
        self.score = score
        self.accepted = accepted
        self.codes = codes
        self.calls: list[tuple[object, bytes | None]] = []

    def evaluate(self, request, *, bild_png: bytes | None = None) -> RubricDecision:
        self.calls.append((request, bild_png))
        values = dict(
            evaluation_id=request.evaluation_id,
            response_id=request.response_id,
            score=self.score,
            confidence=0.9 if self.accepted else 0.4,
            accepted=self.accepted,
            rationale_codes=(
                self.codes
                if self.codes is not None
                else ["rubric_accepted" if self.accepted else "low_confidence"]
            ),
            criterion_results=[
                CriterionEvaluation(
                    criterion_id="authority", awarded_points=self.score,
                    feedback="Nennt den Pfad.", source_refs=["source://authority/1"],
                )
            ],
            misconception_tags=[],
            evaluator_versions={"model": "stub-v1", "prompt": "rubric-prompt-v1"},
            source_refs=["source://authority/1"],
            evidence_ref="openfang://completion/stub-1",
        )
        if self.codes == []:
            # The schema forbids empty codes; a faulty boundary could still send them.
            return RubricDecision.model_construct(**values)
        return RubricDecision(**values)


def _start(service: AdaptiveSessionService, course_id: str, *, mode="training", **blueprint):
    if mode == "training":
        blueprint = {"max_attempts": 3, **blueprint}
    return service.start(
        SessionStartCommand(
            course_id=course_id, course_revision=1, actor_id=ACTOR,
            mode=mode, blueprint=blueprint,
        )
    )


def _richtig(turn) -> dict[str, object]:
    return {"choice_id": next(o.id for o in turn.task.options if o.id.startswith("correct-"))}


def _falsch(turn) -> dict[str, object]:
    return {"choice_id": next(o.id for o in turn.task.options if o.id.startswith("wrong-"))}


def _vor(service, turn, **values):
    return service.vorbewerten(
        VorbewertungCommand(
            session_id=turn.session_id, actor_id=ACTOR,
            expected_session_revision=turn.session_revision, **values,
        )
    )


def _bestaetigen(service, turn, vorbewertung_id: str, revision: int | None = None):
    return service.bestaetigen(
        session_id=turn.session_id, actor_id=ACTOR,
        expected_session_revision=turn.session_revision if revision is None else revision,
        vorbewertung_id=vorbewertung_id,
    )


def _lernstand(factory) -> dict[str, object]:
    with factory() as db:
        counts = {
            model.__name__: db.scalar(select(func.count()).select_from(model)) or 0
            for model in (
                LearningAttempt, LearningResponse, LearningEvaluation, MasteryEvidence,
                ReviewSchedule, Misconception, LearningReviewItem,
            )
        }
        counts["mastery"] = [
            (m.concept_id, m.alpha, m.beta, m.revision)
            for m in db.scalars(select(ConceptMastery)).all()
        ]
        return counts


# ---- Die sieben Tests aus der Aufgabe -------------------------------------------------


def test_vorbewerten_aendert_nichts_am_lernstand(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser(None)
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_richtig(turn)]
    vorher = _lernstand(factory)

    vor = _vor(service, turn, handschrift_png=PNG)

    assert vor.lesbar is True
    assert vor.quelle == "handschrift"
    assert vor.art == "deterministic"
    assert vor.score == 1.0 and vor.accepted is True
    assert vor.gelesen_text == "A"
    assert vor.antwort is None  # Handschrift: das Gelesene steht in gelesen_text
    assert vor.feedback is not None and vor.feedback.state == "correct"
    # The reader sees exactly the public task, never the expected answer.
    assert leser.calls == [(turn.task.model_dump(mode="json"), PNG)]
    assert _lernstand(factory) == vorher
    jetzt = service.current(turn.session_id, actor_id=ACTOR)
    assert jetzt.session_revision == turn.session_revision
    assert jetzt.task == turn.task
    assert jetzt.progress.completed == 0
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        assert row is not None
        assert row.attempt_id == vor.attempt_id
        assert row.bild_sha256 == hashlib.sha256(PNG).hexdigest()
        assert row.bild_png is None  # the reading is the answer; no image is kept
        assert row.gelesen == {"lesbar": True, "roh": {"auswahl": ["A"]}, "text": "A"}
    engine.dispose()


def test_bestaetigen_bucht_einmal(tmp_path: Path) -> None:
    engine, factory, course_id, concept_id = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_richtig(turn)]
    vor = _vor(service, turn, handschrift_png=PNG)

    booked = _bestaetigen(service, turn, vor.id)

    # Same bookkeeping as answer(): +1 for the answer, +1 for appending the next task.
    assert booked.session_revision == turn.session_revision + 2
    assert booked.task is not None and booked.task.id != turn.task.id
    assert booked.progress.completed == 1
    assert booked.feedback is not None and booked.feedback.state == "correct"
    assert booked.audit.mastery_applied is True and booked.audit.readback_verified is True
    with factory() as db:
        assert db.get(ConceptMastery, {"actor_id": ACTOR, "concept_id": concept_id}) is not None
    with pytest.raises(PersistenceConflict):
        _bestaetigen(service, turn, vor.id)  # stale revision
    with pytest.raises(PersistenceConflict, match="vorbewertung not current"):
        _bestaetigen(service, booked, vor.id)  # current revision, but an old attempt
    with factory() as db:
        assert db.scalar(select(func.count(LearningEvaluation.id))) == 1
    engine.dispose()


def test_bestaetigen_unlesbar_abgelehnt(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser(None)
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)

    vor = _vor(service, turn, handschrift_png=PNG)

    assert vor.lesbar is False
    assert vor.score is None and vor.accepted is None and vor.feedback is None
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        assert row is not None and row.lesbar is False and row.answer is None
        assert row.response_id is None and row.evaluation_id is None
    with pytest.raises(ValueError, match="readable"):
        _bestaetigen(service, turn, vor.id)
    assert service.current(turn.session_id, actor_id=ACTOR).session_revision == turn.session_revision
    engine.dispose()


def test_korrektur_bewertet_ohne_leser(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_falsch(turn)]
    gelesen = _vor(service, turn, handschrift_png=PNG)
    assert gelesen.score == 0.0

    korrigiert = _vor(service, turn, answer=_richtig(turn), korrigiert_aus=gelesen.id)

    assert len(leser.calls) == 1
    assert korrigiert.quelle == "getippt"
    assert korrigiert.score == 1.0
    assert korrigiert.antwort == _richtig(turn)
    with factory() as db:
        row = db.get(LearningVorbewertung, korrigiert.id)
        assert row.korrigiert_aus == gelesen.id
        assert row.bild_sha256 is None and row.gelesen is None
    with pytest.raises(ValueError, match="korrigiert_aus"):
        _vor(service, turn, answer=_richtig(turn), korrigiert_aus=str(uuid4()))
    engine.dispose()


def test_rubrik_typ_lehnt_korrektur_ab(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("case")])
    rubrik = _Rubrik()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=rubrik)
    turn = _start(service, course_id)
    vor = _vor(service, turn, handschrift_png=PNG)
    assert vor.art == "rubric" and vor.quelle == "handschrift"
    request, bild = rubrik.calls[0]
    assert request.response == {
        "kind": "handschrift", "vorbewertung_id": vor.id, "bild_sha256": hashlib.sha256(PNG).hexdigest(),
    }
    assert bild == PNG

    with pytest.raises(ValueError):
        _vor(service, turn, answer={"text": "Der Pfad."}, korrigiert_aus=vor.id)
    with pytest.raises(ValueError):
        _vor(service, turn, answer={"text": "Der Pfad."})
    assert len(rubrik.calls) == 1
    engine.dispose()


def test_pruefung_lehnt_vorbewertung_ab(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser({"choice_id": "correct-1"})
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(
        service, course_id, mode="exam",
        item_count=2, item_types=["single_choice"], difficulty_bands=[],
    )

    with pytest.raises(PermissionError, match="exam"):
        _vor(service, turn, handschrift_png=PNG)
    with pytest.raises(PermissionError, match="exam"):
        _vor(service, turn, answer=_richtig(turn))
    assert leser.calls == []
    engine.dispose()


def test_ueberarbeiten_dann_bestaetigen_zaehlt_nur_letzte(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_falsch(turn), _richtig(turn)]
    erste = _vor(service, turn, handschrift_png=PNG)
    zweite = _vor(service, turn, handschrift_png=PNG + b"2")
    assert (erste.score, zweite.score) == (0.0, 1.0)

    booked = _bestaetigen(service, turn, zweite.id)

    with factory() as db:
        evaluations = db.scalars(select(LearningEvaluation)).all()
        assert [e.score for e in evaluations] == [1.0]
        assert db.scalar(select(func.count(LearningVorbewertung.id))) == 2
        # The wrong first try leaves no trace in the learning state.
        assert db.scalar(select(func.count(Misconception.id))) == 0
    assert booked.feedback.state == "correct"
    engine.dispose()


# ---- Zusaetzliche Absicherungen -------------------------------------------------------


def test_bestaetigen_uebernimmt_gespeicherte_ids_ohne_neuen_aufruf(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("open"), _rubric_item("open")])
    rubrik = _Rubrik(score=1.0)
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=rubrik)
    turn = _start(service, course_id)
    vor = _vor(service, turn, handschrift_png=PNG)
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        response_id, evaluation_id = row.response_id, row.evaluation_id

    booked = _bestaetigen(service, turn, vor.id)

    assert len(rubrik.calls) == 1
    assert booked.audit.response_id == response_id
    assert booked.audit.evaluation_id == evaluation_id
    with factory() as db:
        response = db.get(LearningResponse, response_id)
        assert response is not None and response.answer["kind"] == "handschrift"
        assert response.answer["vorbewertung_id"] == vor.id
        assert db.get(LearningEvaluation, evaluation_id).response_id == response_id
    assert booked.feedback.criteria and booked.feedback.criteria[0].criterion_id == "authority"
    engine.dispose()


def test_deterministisch_bestaetigen_ruft_leser_nicht_erneut(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_richtig(turn)]
    vor = _vor(service, turn, handschrift_png=PNG)
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        ids = (row.response_id, row.evaluation_id)

    booked = _bestaetigen(service, turn, vor.id)

    assert len(leser.calls) == 1
    assert (booked.audit.response_id, booked.audit.evaluation_id) == ids
    engine.dispose()


def test_gespeicherte_bewertung_kommt_verlustfrei_zurueck(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("open")])
    service = AdaptiveSessionService(
        factory, clock=lambda: NOW, rubric_evaluator=_Rubrik(score=0.7, accepted=False)
    )
    gesehen = []
    original = service._bewerten

    def _merken(*args, **kwargs):
        gesehen.append(original(*args, **kwargs))
        return gesehen[-1]

    service._bewerten = _merken
    turn = _start(service, course_id)
    vor = _vor(service, turn, handschrift_png=PNG)

    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        # The stored grading is the graded one plus the recorded grader.
        erwartet = replace(
            gesehen[0], evaluator_versions={**gesehen[0].evaluator_versions, "grader": "server"}
        )
        assert service._bewertung_aus(row) == erwartet
    engine.dispose()


def test_rubrik_ohne_begruendungscode_faellt_auf_festen_code(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("case"), _rubric_item("case")])
    service = AdaptiveSessionService(
        factory, clock=lambda: NOW, rubric_evaluator=_Rubrik(accepted=False, codes=[])
    )
    turn = _start(service, course_id)

    vor = _vor(service, turn, handschrift_png=PNG)
    assert vor.feedback is not None and vor.feedback.state == "needs_review"
    _bestaetigen(service, turn, vor.id)

    with factory() as db:
        review = db.scalar(select(LearningReviewItem))
        assert review is not None and review.reason_code == "rubric_not_accepted"
    engine.dispose()


def test_penecho_canvas_extern_geht_bei_vorschau_trotzdem_an_die_rubrik(tmp_path: Path) -> None:
    spec = _rubric_item("penecho_canvas")
    spec["scoring_config"] = {**spec["scoring_config"], "presentation": {"canvas_href": "x"}}
    engine, factory, course_id, _ = _store(tmp_path, [spec])
    rubrik = _Rubrik()
    service = AdaptiveSessionService(
        factory, clock=lambda: NOW, rubric_evaluator=rubrik,
        external_rubric_types=frozenset({"penecho_canvas"}),
    )
    turn = _start(service, course_id)

    vor = _vor(service, turn, handschrift_png=PNG)

    assert vor.art == "rubric" and vor.lesbar is True
    assert rubrik.calls[0][0].response["kind"] == "handschrift"
    assert rubrik.calls[0][1] == PNG
    engine.dispose()


def test_code_typ_hat_keine_vorbewertung(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("code")])
    rubrik = _Rubrik()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, rubric_evaluator=rubrik)
    turn = _start(service, course_id)

    with pytest.raises(ValueError, match="code"):
        _vor(service, turn, handschrift_png=PNG)
    assert rubrik.calls == []
    engine.dispose()


def test_befehl_braucht_genau_eine_antwortform() -> None:
    session_id = str(uuid4())
    basis = dict(session_id=session_id, actor_id=ACTOR, expected_session_revision=1)
    with pytest.raises(ValidationError):
        VorbewertungCommand(**basis)
    with pytest.raises(ValidationError):
        VorbewertungCommand(**basis, answer={"choice_id": "a"}, handschrift_png=PNG)
    with pytest.raises(ValidationError):
        VorbewertungCommand(**basis, handschrift_png=PNG, korrigiert_aus=str(uuid4()))
    assert VorbewertungCommand(**basis, answer={"choice_id": "a"}).handschrift_png is None


def test_vorbewerten_prueft_revision_und_veraltete_vorbewertung(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2), _choice(3)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    with pytest.raises(PersistenceConflict, match="revision"):
        service.vorbewerten(
            VorbewertungCommand(
                session_id=turn.session_id, actor_id=ACTOR,
                expected_session_revision=turn.session_revision + 1, answer=_richtig(turn),
            )
        )
    vor = _vor(service, turn, answer=_richtig(turn))
    # The learner answers the same task the normal way: the preview is now stale.
    answered = service.answer(
        AnswerCommand(
            session_id=turn.session_id, actor_id=ACTOR,
            expected_session_revision=turn.session_revision, answer=_falsch(turn),
        )
    )
    with pytest.raises(PersistenceConflict, match="vorbewertung not current"):
        _bestaetigen(service, answered, vor.id)
    with pytest.raises(ValueError, match="korrigiert_aus"):
        _vor(service, answered, answer=_richtig(answered), korrigiert_aus=vor.id)
    engine.dispose()


def test_handschrift_ohne_leser_ist_nicht_konfiguriert(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(service, course_id)

    with pytest.raises(RuntimeError, match="reader"):
        _vor(service, turn, handschrift_png=PNG)
    engine.dispose()


def test_migration_0020_legt_tabelle_an(tmp_path: Path) -> None:
    from spaces.learning.deployment.migrate import migrate
    from spaces.learning.services.db.session import create_learning_engine

    url = f"sqlite:///{tmp_path / 'm.db'}"
    migrate(url)
    engine = create_learning_engine(url)
    try:
        with engine.connect() as c:
            assert (
                c.exec_driver_sql("SELECT version_num FROM learning_alembic_version").scalar_one()
                == "0021_feste_liste"
            )
        spalten = {col["name"] for col in inspect(engine).get_columns("learning_vorbewertungen")}
        assert spalten == {
            column.name for column in LearningVorbewertung.__table__.columns
        }
    finally:
        engine.dispose()


# ---- Nachbesserung 1: Bild fuer die Pruefung, eindeutige Fehler -----------------------


def test_unakzeptierte_rubrik_handschrift_behaelt_bild_fuer_pruefung(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_rubric_item("open"), _rubric_item("open")])
    service = AdaptiveSessionService(
        factory, clock=lambda: NOW, rubric_evaluator=_Rubrik(accepted=False)
    )
    turn = _start(service, course_id)
    vor = _vor(service, turn, handschrift_png=PNG)

    _bestaetigen(service, turn, vor.id)

    with factory() as db:
        review = db.scalar(select(LearningReviewItem))
        evaluation = db.get(LearningEvaluation, review.evaluation_id)
        response = db.get(LearningResponse, evaluation.response_id)
        assert response.answer == {
            "kind": "handschrift",
            "vorbewertung_id": vor.id,
            "bild_sha256": hashlib.sha256(PNG).hexdigest(),
        }
        stored = db.get(LearningVorbewertung, response.answer["vorbewertung_id"])
        assert stored.bild_png == PNG
        assert hashlib.sha256(stored.bild_png).hexdigest() == response.answer["bild_sha256"]
    engine.dispose()


def test_rubrik_vorschau_ohne_rubrik_ist_nicht_konfiguriert(tmp_path: Path) -> None:
    spec = _rubric_item("penecho_canvas")
    spec["scoring_config"] = {**spec["scoring_config"], "presentation": {"canvas_href": "x"}}
    engine, factory, course_id, _ = _store(tmp_path, [spec])
    service = AdaptiveSessionService(
        factory, clock=lambda: NOW, external_rubric_types=frozenset({"penecho_canvas"})
    )
    turn = _start(service, course_id)

    with pytest.raises(RuntimeError, match="rubric evaluator"):
        _vor(service, turn, handschrift_png=PNG)
    with factory() as db:
        assert db.scalar(select(func.count(LearningVorbewertung.id))) == 0
    engine.dispose()


def test_leser_antwort_die_der_bewerter_ablehnt_ist_unlesbar(tmp_path: Path, monkeypatch) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser({"choice_id": "correct-1"})
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)

    def _ablehnen(item, answer):
        raise ValueError("answer does not fit the item")

    monkeypatch.setattr(session_modul, "score_response", _ablehnen)
    vor = _vor(service, turn, handschrift_png=PNG)

    assert vor.lesbar is False and vor.score is None and vor.feedback is None
    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        assert row.lesbar is False and row.response_id is None
        assert row.gelesen == {"lesbar": True, "roh": {"auswahl": ["A"]}, "text": "A"}
    with pytest.raises(ValueError, match="readable"):
        _bestaetigen(service, turn, vor.id)
    engine.dispose()


def test_leser_lesbar_ohne_antwort_ist_unlesbar(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])

    class _OhneAntwort:
        def lesen(self, task, png):
            return GelesenV1(lesbar=True, roh={"auswahl": ["Z"]}, text="Z"), None

    service = AdaptiveSessionService(
        factory, clock=lambda: NOW, handschrift_leser=_OhneAntwort()
    )
    turn = _start(service, course_id)

    vor = _vor(service, turn, handschrift_png=PNG)

    with factory() as db:
        row = db.get(LearningVorbewertung, vor.id)
        assert row.lesbar is False and row.gelesen["text"] == "Z"
    assert vor.lesbar is row.lesbar is False
    assert vor.score is None and vor.accepted is None and vor.feedback is None
    engine.dispose()


def test_fremde_person_wird_abgelehnt(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(service, course_id)
    vor = _vor(service, turn, answer=_richtig(turn))

    with pytest.raises(PermissionError, match="another actor"):
        service.vorbewerten(
            VorbewertungCommand(
                session_id=turn.session_id, actor_id="fremd",
                expected_session_revision=turn.session_revision, answer=_richtig(turn),
            )
        )
    with pytest.raises(PermissionError, match="another actor"):
        service.bestaetigen(
            session_id=turn.session_id, actor_id="fremd",
            expected_session_revision=turn.session_revision, vorbewertung_id=vor.id,
        )
    with pytest.raises(PermissionError, match="another actor"):
        service.answer(
            AnswerCommand(
                session_id=turn.session_id, actor_id="fremd",
                expected_session_revision=turn.session_revision, answer=_richtig(turn),
            )
        )
    engine.dispose()


def test_vorbewertung_aus_anderer_sitzung_ist_unbekannt(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    erste = _start(service, course_id)
    zweite = _start(service, course_id)
    vor = _vor(service, erste, answer=_richtig(erste))

    with pytest.raises(LookupError, match="vorbewertung"):
        _bestaetigen(service, zweite, vor.id)
    with pytest.raises(LookupError, match="vorbewertung"):
        _bestaetigen(service, zweite, str(uuid4()))
    engine.dispose()


def test_bestaetigen_in_pruefung_abgelehnt(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    turn = _start(
        service, course_id, mode="exam",
        item_count=2, item_types=["single_choice"], difficulty_bands=[],
    )

    with pytest.raises(PermissionError, match="exam"):
        _bestaetigen(service, turn, str(uuid4()))
    assert service.current(turn.session_id, actor_id=ACTOR).session_revision == turn.session_revision
    engine.dispose()
