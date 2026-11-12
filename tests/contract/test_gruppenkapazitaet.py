"""`learning_group_capacity`: wie viele Mappen eine neue Gruppenflaeche des Kurses fasst (lesend, fuer den Fork).

Die Aufgabenliste ist die, die eine neue Listensitzung des Kurses bekaeme (wie die Huelle sie startet), die Hoehen
kommen aus derselben Regel wie in der Huelle (`huelle.medien`), die Plaetze aus `anordnung.gruppen_kapazitaet`.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from spaces.learning.bridge.dispatcher import InMemoryReceiptStore, LearningDispatcher
from spaces.learning.huelle.anordnung import HOEHE, HOEHE_GROSS, gruppen_kapazitaet, hoehen
from spaces.learning.huelle.bruecke import _gross, _listeneintraege
from spaces.learning.huelle.medien import aufgaben_hoehen
from spaces.learning.mcp import server
from spaces.learning.mcp.tools.sessions import build_session_gateways
from spaces.learning.services.adaptive_engine.models import AdaptiveConcept, AdaptiveItem, ItemConcept
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.db.models import Base


def _frage(index: int) -> dict[str, object]:
    return {
        "item_type": "single_choice",
        "expected_answer": {"choice_id": "a"},
        "scoring_config": {
            "prompt": f"Frage {index}?",
            "options": [{"id": "a", "label": "Ja"}, {"id": "b", "label": "Nein"}],
            "source_refs": [f"source://course/{index}"],
            "hints": [],
            "representation": "quiz",
            "remediation_tags": [],
        },
    }


def _fenster(index: int, bild: str) -> dict[str, object]:
    return {
        "item_type": "fill_in",
        "expected_answer": {"value": "ls"},
        "scoring_config": {
            "prompt": f"Befehl {index}?",
            "source_refs": [f"source://course/{index}"],
            "hints": [],
            "presentation": {"template": "$ ___", "shell": "bash", "image": bild},
        },
    }


def _zeichnen(index: int) -> dict[str, object]:
    return {
        "item_type": "penecho_canvas",
        "expected_answer": {},
        "scoring_config": {"prompt": f"Zeichne {index}.", "source_refs": [f"source://course/{index}"],
                           "hints": []},
    }


def _kurs(tmp_path: Path, aufgaben: list[dict[str, object]], *, revision: int = 1):
    engine = create_engine(f"sqlite:///{tmp_path / 'kapazitaet.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, connection_record) -> None:
        del connection_record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    course_id = str(uuid4())
    concept_id = str(uuid4())
    with factory() as db, db.begin():
        db.add(AdaptiveConcept(id=concept_id, course_id=course_id, course_revision=revision,
                               concept_key="k", title="Begriff"))
        for platz, aufgabe in enumerate(aufgaben, start=1):
            item_id = str(uuid4())
            db.add(AdaptiveItem(id=item_id, course_id=course_id, course_revision=revision,
                                activity_id=f"activity-{platz}", difficulty=0.3, quality_status="approved",
                                rubric=[], listen_platz=platz, **aufgabe))
            db.add(ItemConcept(item_id=item_id, concept_id=concept_id, course_id=course_id,
                               course_revision=revision))
    return engine, factory, course_id


def _dispatcher(factory) -> LearningDispatcher:
    service = AdaptiveSessionService(factory, external_rubric_types=frozenset({"penecho_canvas"}))
    return LearningDispatcher(gateways=build_session_gateways(service), receipts=InMemoryReceiptStore())


def _call(dispatcher: LearningDispatcher, name: str, arguments: dict[str, object]) -> dict:
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        dispatcher=dispatcher,
    )
    assert response is not None
    return json.loads(response["result"]["content"][0]["text"])


def _event(event_type: str, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "event_type": event_type,
        "invocation_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "actor": {"actor_id": "learnhouse-einladung", "actor_type": "system"},
        "payload": {},
    }
    value.update(updates)
    return value


def _kapazitaet(dispatcher: LearningDispatcher, course_id: str) -> dict:
    return _call(dispatcher, "learning_group_capacity", _event("learning.group.capacity", course_id=course_id))


def test_server_listet_das_werkzeug() -> None:
    namen = {t["name"] for t in server.TOOLS}
    assert "learning_group_capacity" in namen


def test_kapazitaet_16_fragen_zehn_plaetze(tmp_path: Path) -> None:
    engine, factory, course_id = _kurs(tmp_path, [_frage(i) for i in range(1, 17)])
    antwort = _kapazitaet(_dispatcher(factory), course_id)
    assert antwort["state"] == "completed", antwort
    assert antwort["evidence"]["evidence_type"] == "application_readback"
    assert antwort["result"] == {"kapazitaet": 10, "aufgaben": 16, "course_revision": 1}
    engine.dispose()


def test_kapazitaet_rechnet_wie_die_huelle(tmp_path: Path) -> None:
    """Dieselbe Liste und dieselben Hoehen wie die Mappe, die die Huelle fuer eine neue Listensitzung setzt."""
    aufgaben = ([_frage(i) for i in range(1, 9)] + [_fenster(9, "/lernsequenz-medien/fenster-9.png")]
                + [_fenster(10, "https://anderswo.example/bild.png")] + [_zeichnen(11)]
                + [_frage(i) for i in range(12, 18)])
    engine, factory, course_id = _kurs(tmp_path, aufgaben)
    dispatcher = _dispatcher(factory)
    antwort = _kapazitaet(dispatcher, course_id)
    assert antwort["state"] == "completed", antwort
    # Wie die Huelle: Listensitzung starten (TRAINING_BLUEPRINT), Liste lesen, `_listeneintraege`, `_gross`.
    gestartet = _call(dispatcher, "learning_session_start", _event(
        "learning.session.start", actor={"actor_id": "student-1:g7", "actor_type": "local_user"},
        course_id=course_id, idempotency_key=f"start-{uuid4()}",
        payload={"course_revision": 1, "mode": "training", "blueprint": {"max_attempts": 20}, "ablauf": "liste"}))
    assert gestartet["state"] == "completed", gestartet
    liste = _call(dispatcher, "learning_task_list", _event(
        "learning.task.list", actor={"actor_id": "student-1:g7", "actor_type": "local_user"},
        session_id=gestartet["result"]["session"]["session_id"]))
    eintraege = _listeneintraege(liste["result"])
    h = hoehen([_gross(e["task"]) for e in eintraege])
    assert h == [HOEHE] * 8 + [HOEHE_GROSS, HOEHE, HOEHE_GROSS] + [HOEHE] * 6
    assert antwort["result"] == {"kapazitaet": gruppen_kapazitaet(h), "aufgaben": 17, "course_revision": 1}
    engine.dispose()


def test_neueste_freigegebene_revision(tmp_path: Path) -> None:
    engine, factory, course_id = _kurs(tmp_path, [_frage(i) for i in range(1, 4)])
    with factory() as db, db.begin():
        concept_id = str(uuid4())
        db.add(AdaptiveConcept(id=concept_id, course_id=course_id, course_revision=2, concept_key="k",
                               title="Begriff"))
        for platz in range(1, 17):
            item_id = str(uuid4())
            db.add(AdaptiveItem(id=item_id, course_id=course_id, course_revision=2, activity_id=f"a-{platz}",
                                difficulty=0.3, quality_status="approved", rubric=[], listen_platz=platz,
                                **_frage(platz)))
            db.add(ItemConcept(item_id=item_id, concept_id=concept_id, course_id=course_id, course_revision=2))
    antwort = _kapazitaet(_dispatcher(factory), course_id)
    assert antwort["result"] == {"kapazitaet": 10, "aufgaben": 16, "course_revision": 2}
    engine.dispose()


def test_unbekannter_kurs_oder_ohne_kurs_abgelehnt(tmp_path: Path) -> None:
    engine, factory, _ = _kurs(tmp_path, [_frage(1)])
    dispatcher = _dispatcher(factory)
    unbekannt = _kapazitaet(dispatcher, str(uuid4()))
    assert unbekannt["state"] == "rejected" and unbekannt["error"]["code"] == "invalid_session_request"
    ohne = _call(dispatcher, "learning_group_capacity", _event("learning.group.capacity"))
    assert ohne["state"] == "rejected" and ohne["error"]["code"] == "invalid_session_request"
    engine.dispose()


def test_aufgaben_hoehen_aendert_die_eingabe_nicht() -> None:
    roh = {"id": "t", "type": "fill_in", "presentation": {"image": "/lernsequenz-medien/a.png"}}
    assert aufgaben_hoehen([roh, {"id": "u", "type": "penecho_canvas"}, {"id": "v", "type": "single_choice"}]) == [
        HOEHE_GROSS, HOEHE_GROSS, HOEHE]
    assert roh["presentation"]["image"] == "/lernsequenz-medien/a.png"
    assert aufgaben_hoehen([{"id": "w", "presentation": {"image": "file:///etc/x.png"}}]) == [HOEHE]
