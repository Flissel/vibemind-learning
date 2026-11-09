"""Feste Liste: all attempts exist from the start; answers address one attempt by id."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ItemConcept,
    LearningAttempt,
)
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    AnswerCommand,
    SessionStartCommand,
    VorbewertungCommand,
)
from spaces.learning.services.db.models import Base
from spaces.learning.services.db.repository import PersistenceConflict

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
ACTOR = "lernende-1"


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
            "remediation_tags": [],
        },
        "rubric": [],
    }


def _store(
    tmp_path: Path, plaetze: list[int | None], ids: list[str] | None = None,
    erstellt: list[datetime] | None = None,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'liste.db'}")

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
                concept_key="k", title="Begriff",
            )
        )
        for index, platz in enumerate(plaetze, start=1):
            item_id = str(uuid4())
            db.add(
                AdaptiveItem(
                    id=item_id, course_id=course_id, course_revision=1,
                    activity_id=ids[index - 1] if ids else f"activity-{index}",
                    difficulty=0.5, quality_status="approved", listen_platz=platz,
                    **({"created_at": erstellt[index - 1]} if erstellt else {}),
                    **_choice(index),
                )
            )
            db.add(
                ItemConcept(
                    item_id=item_id, concept_id=concept_id,
                    course_id=course_id, course_revision=1,
                )
            )
    return factory, course_id


def _service(factory) -> AdaptiveSessionService:
    return AdaptiveSessionService(factory, clock=lambda: NOW)


def _start(service, course_id, *, ablauf="liste", mode="training", blueprint=None):
    kwargs = {} if ablauf is None else {"ablauf": ablauf}
    return service.start(
        SessionStartCommand(
            course_id=course_id, course_revision=1, actor_id=ACTOR, mode=mode,
            blueprint={"max_attempts": 3} if blueprint is None else blueprint, **kwargs,
        )
    )


def _versuche(factory, turn) -> list[LearningAttempt]:
    with factory() as db:
        return list(
            db.scalars(
                select(LearningAttempt)
                .where(LearningAttempt.session_id == turn.session_id)
                .order_by(LearningAttempt.ordinal)
            )
        )


def _richtig(factory, attempt: LearningAttempt) -> dict[str, object]:
    with factory() as db:
        return dict(db.get(AdaptiveItem, attempt.item_id).expected_answer)


def _vor(service, turn, attempt, factory):
    return service.vorbewerten(
        VorbewertungCommand(
            session_id=turn.session_id, actor_id=ACTOR, expected_session_revision=1,
            attempt_id=attempt.id, answer=_richtig(factory, attempt),
        )
    )


def _bestaetigen(service, turn, attempt, vorbewertung):
    return service.bestaetigen(
        session_id=turn.session_id, actor_id=ACTOR, expected_session_revision=1,
        vorbewertung_id=vorbewertung.id, attempt_id=attempt.id,
    )


def _antwort(service, turn, attempt, factory):
    return service.answer(
        AnswerCommand(
            session_id=turn.session_id, actor_id=ACTOR, expected_session_revision=1,
            attempt_id=attempt.id, answer=_richtig(factory, attempt),
        )
    )


def test_start_liste_legt_alle_versuche_an(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [3, 1, 2])
    turn = _start(_service(factory), course_id)

    versuche = _versuche(factory, turn)
    with factory() as db:
        folge = [db.get(AdaptiveItem, v.item_id).listen_platz for v in versuche]
    assert [v.ordinal for v in versuche] == [1, 2, 3]
    assert folge == [1, 2, 3]
    assert all(v.selection_reason["mode"] == "liste" for v in versuche)
    assert [v.selection_reason["ordinal"] for v in versuche] == [1, 2, 3]
    assert turn.ablauf == "liste"
    assert turn.task is not None and turn.task.id == versuche[0].item_id
    assert turn.session_revision == 2


def test_liste_ohne_listen_platz_gleichzeitig_folgt_activity_id(tmp_path: Path) -> None:
    gleich = [datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)] * 3
    factory, course_id = _store(tmp_path, [None, None, None], ["c", "a", "b"], gleich)
    turn = _start(_service(factory), course_id)

    with factory() as db:
        folge = [db.get(AdaptiveItem, v.item_id).activity_id for v in _versuche(factory, turn)]
    assert folge == ["a", "b", "c"]


def test_liste_ohne_listen_platz_folgt_der_anlagezeit(tmp_path: Path) -> None:
    # Vor A2 freigegebene Kurse: kein listen_platz; die Autorenreihenfolge steckt in created_at.
    zeiten = [datetime(2026, 1, 1, 12, 0, s, tzinfo=timezone.utc) for s in (1, 2, 3)]
    factory, course_id = _store(tmp_path, [None, None, None], ["c", "a", "b"], zeiten)
    turn = _start(_service(factory), course_id)

    with factory() as db:
        folge = [db.get(AdaptiveItem, v.item_id).activity_id for v in _versuche(factory, turn)]
    assert folge == ["c", "a", "b"]


def test_listen_platz_gewinnt_vor_der_anlagezeit(tmp_path: Path) -> None:
    zeiten = [datetime(2026, 1, 1, 12, 0, s, tzinfo=timezone.utc) for s in (1, 2, 3)]
    factory, course_id = _store(tmp_path, [3, None, 1], ["x", "y", "z"], zeiten)
    turn = _start(_service(factory), course_id)

    with factory() as db:
        folge = [db.get(AdaptiveItem, v.item_id).activity_id for v in _versuche(factory, turn)]
    assert folge == ["z", "x", "y"]  # Plaetze 1, 3; danach der Eintrag ohne Platz


def test_abgeben_in_beliebiger_reihenfolge(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id)
    v1, v2, v3 = _versuche(factory, turn)

    vor2 = _vor(service, turn, v2, factory)
    vor1 = _vor(service, turn, v1, factory)
    _bestaetigen(service, turn, v1, vor1)
    zwischen = _bestaetigen(service, turn, v2, vor2)

    assert zwischen.state == "active"
    assert service.current(turn.session_id, actor_id=ACTOR).task.id == v3.item_id
    ende = _antwort(service, turn, v3, factory)
    assert ende.state == "completed"
    assert ende.summary is not None and ende.summary.attempted == 3


def test_liste_braucht_attempt_id(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id)
    erster = _versuche(factory, turn)[0]

    with pytest.raises(ValueError, match="list sessions need attempt_id"):
        service.answer(
            AnswerCommand(
                session_id=turn.session_id, actor_id=ACTOR, expected_session_revision=1,
                answer=_richtig(factory, erster),
            )
        )


def test_bestaetigter_versuch_nimmt_nichts_mehr(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id)
    v1 = _versuche(factory, turn)[0]
    _antwort(service, turn, v1, factory)

    with pytest.raises(PersistenceConflict, match="already answered"):
        _vor(service, turn, v1, factory)


def test_fremder_versuch_abgelehnt(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id)
    anderer = _start(service, course_id)
    fremd = _versuche(factory, anderer)[0]

    with pytest.raises(LookupError, match="attempt is unknown"):
        _vor(service, turn, fremd, factory)


def test_liste_nur_beim_ueben(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])

    with pytest.raises(ValueError, match="list sessions are training only"):
        _start(
            _service(factory), course_id, mode="exam",
            blueprint={"item_count": 2, "item_types": [], "difficulty_bands": []},
        )


def test_anpassend_bleibt_wie_bisher(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id, ablauf=None)

    assert turn.ablauf == "anpassend"
    assert len(_versuche(factory, turn)) == 1
    erster = _versuche(factory, turn)[0]
    with pytest.raises(PersistenceConflict, match="revision conflict"):
        service.answer(
            AnswerCommand(
                session_id=turn.session_id, actor_id=ACTOR,
                expected_session_revision=turn.session_revision + 5,
                answer=_richtig(factory, erster),
            )
        )
    assert turn.session_revision == 2


def test_anpassend_fremde_attempt_id_ist_nicht_aktuell(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id, ablauf=None)
    erster = _versuche(factory, turn)[0]

    with pytest.raises(PersistenceConflict, match="vorbewertung not current"):
        service.vorbewerten(
            VorbewertungCommand(
                session_id=turn.session_id, actor_id=ACTOR,
                expected_session_revision=turn.session_revision,
                attempt_id=str(uuid4()), answer=_richtig(factory, erster),
            )
        )


def test_aufgabenliste(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id)
    v1, v2, v3 = _versuche(factory, turn)

    vor2 = _vor(service, turn, v2, factory)
    vor1 = _vor(service, turn, v1, factory)
    _bestaetigen(service, turn, v1, vor1)
    liste = service.aufgabenliste(turn.session_id, actor_id=ACTOR)

    assert [e.nummer for e in liste] == [1, 2, 3]
    assert [e.attempt_id for e in liste] == [v1.id, v2.id, v3.id]
    assert [e.status for e in liste] == ["bestaetigt", "offen", "offen"]
    assert liste[0].vorbewertung is not None and liste[0].vorbewertung.id == vor1.id
    assert liste[1].vorbewertung == vor2
    assert liste[2].vorbewertung is None
    assert liste[1].task.id == v2.item_id
    assert liste[1].task.expected_answer is None


def test_aufgabenliste_nur_fuer_listen(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id, ablauf=None)

    with pytest.raises(ValueError, match="only list sessions have a task list"):
        service.aufgabenliste(turn.session_id, actor_id=ACTOR)
    with pytest.raises(PermissionError):
        service.aufgabenliste(turn.session_id, actor_id="jemand-anders")
    with pytest.raises(LookupError):
        service.aufgabenliste(str(uuid4()), actor_id=ACTOR)


def test_nachlesen_in_listen_vertraegt_zwischenbuchung(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id)
    v1, v2, _ = _versuche(factory, turn)

    # A is booked; B commits before A's readback runs. A must still be returned.
    gebucht_a = service.answer(
        AnswerCommand(
            session_id=turn.session_id, actor_id=ACTOR, expected_session_revision=1,
            attempt_id=v1.id, answer=_richtig(factory, v1),
        )
    )
    _antwort(service, turn, v2, factory)

    ergebnis = service._with_readback(gebucht_a.model_copy(update={"audit": gebucht_a.audit}))
    assert ergebnis.audit.readback_verified is True
    assert ergebnis.audit.response_id == gebucht_a.audit.response_id


def test_nachlesen_anpassend_verlangt_gleiche_revision(tmp_path: Path) -> None:
    factory, course_id = _store(tmp_path, [1, 2, 3])
    service = _service(factory)
    turn = _start(service, course_id, ablauf=None)
    erster = _versuche(factory, turn)[0]
    service.answer(
        AnswerCommand(
            session_id=turn.session_id, actor_id=ACTOR,
            expected_session_revision=turn.session_revision, answer=_richtig(factory, erster),
        )
    )

    with pytest.raises(PersistenceConflict, match="readback failed"):
        service._with_readback(turn)
