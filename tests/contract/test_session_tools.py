from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from spaces.learning.bridge.dispatcher import InMemoryReceiptStore, LearningDispatcher
from spaces.learning.contracts.events import LearningToolName
from spaces.learning.mcp import server
from spaces.learning.mcp.tools.sessions import build_session_gateways
from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ItemConcept,
)
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.db.models import Base
from spaces.learning.services.notizen.store import NoteStore


def _store(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp-session.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, connection_record) -> None:
        del connection_record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    course_id = str(uuid4())
    concept_id = str(uuid4())
    item_id = str(uuid4())
    with factory() as db, db.begin():
        db.add(
            AdaptiveConcept(
                id=concept_id,
                course_id=course_id,
                course_revision=1,
                concept_key="authority",
                title="Authority",
            )
        )
        db.add(
            AdaptiveItem(
                id=item_id,
                course_id=course_id,
                course_revision=1,
                activity_id="activity-1",
                item_type="single_choice",
                difficulty=0.3,
                quality_status="approved",
                expected_answer={"choice_id": "a"},
                scoring_config={
                    "prompt": "Welche Quelle ist autorisiert?",
                    "options": [
                        {"id": "a", "label": "Freigegebene Kursquelle"},
                        {"id": "b", "label": "Unbelegte Erinnerung"},
                    ],
                    "source_refs": ["source://course/1"],
                    "hints": ["Pruefe die Freigabe."],
                    "representation": "quiz",
                    "remediation_tags": [],
                },
                rubric=[],
            )
        )
        db.add(
            ItemConcept(
                item_id=item_id,
                concept_id=concept_id,
                course_id=course_id,
                course_revision=1,
            )
        )
    return engine, factory, course_id


def _event(event_type: str, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "event_type": event_type,
        "invocation_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "actor": {"actor_id": "student-mcp", "actor_type": "local_user"},
        "payload": {},
    }
    value.update(updates)
    return value


def _call(dispatcher: LearningDispatcher, name: str, arguments: dict[str, object]):
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        dispatcher=dispatcher,
    )
    assert response is not None
    return json.loads(response["result"]["content"][0]["text"])


def test_session_tools_emit_only_readback_verified_task_and_result_intents(
    tmp_path: Path,
) -> None:
    engine, factory, course_id = _store(tmp_path)
    service = AdaptiveSessionService(factory)
    dispatcher = LearningDispatcher(
        gateways=build_session_gateways(service),
        receipts=InMemoryReceiptStore(),
    )
    started = _call(
        dispatcher,
        "learning_session_start",
        _event(
            "learning.session.start",
            course_id=course_id,
            idempotency_key="session-start-1",
            payload={
                "course_revision": 1,
                "mode": "training",
                "blueprint": {"max_attempts": 1},
            },
        ),
    )

    assert started["state"] == "completed"
    assert started["evidence"]["evidence_type"] == "application_readback"
    assert started["ui_intent"]["kind"] == "open_task"
    assert started["ui_intent"]["aggregate_revision"] == started["aggregate"]["revision"]
    session_id = started["result"]["session"]["session_id"]
    revision = started["aggregate"]["revision"]

    hinted = _call(
        dispatcher,
        "learning_hint_request",
        _event("learning.hint.request", session_id=session_id),
    )
    assert hinted["state"] == "completed"
    assert hinted["result"]["hint"] == "Pruefe die Freigabe."

    answered = _call(
        dispatcher,
        "learning_task_answer",
        _event(
            "learning.task.answer",
            session_id=session_id,
            expected_revision=revision,
            idempotency_key="answer-1",
            payload={"answer": {"choice_id": "a"}},
        ),
    )
    assert answered["state"] == "completed"
    assert answered["result"]["session"]["state"] == "completed"
    assert answered["result"]["session"]["summary"]["correct"] == 1
    assert answered["ui_intent"]["kind"] == "show_result"
    assert answered["ui_intent"]["aggregate_revision"] == answered["aggregate"]["revision"]
    engine.dispose()


def test_session_tool_rejects_stale_revision_without_success_intent(tmp_path: Path) -> None:
    engine, factory, course_id = _store(tmp_path)
    service = AdaptiveSessionService(factory)
    gateways = build_session_gateways(service)
    dispatcher = LearningDispatcher(gateways=gateways, receipts=InMemoryReceiptStore())
    started = _call(
        dispatcher,
        LearningToolName.SESSION_START.value,
        _event(
            "learning.session.start",
            course_id=course_id,
            idempotency_key="session-start-stale",
            payload={"course_revision": 1, "mode": "training", "blueprint": {"max_attempts": 2}},
        ),
    )
    rejected = _call(
        dispatcher,
        LearningToolName.TASK_ANSWER.value,
        _event(
            "learning.task.answer",
            session_id=started["result"]["session"]["session_id"],
            expected_revision=1,
            idempotency_key="answer-stale",
            payload={"answer": {"choice_id": "a"}},
        ),
    )
    assert rejected["state"] == "rejected"
    assert rejected["error"]["code"] == "session_revision_conflict"
    assert "ui_intent" not in rejected
    engine.dispose()


# ---- task_preview / task_confirm -------------------------------------------------------

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nhandschrift-test").decode("ascii")


def _start(dispatcher: LearningDispatcher, course_id: str, mode: str = "training") -> dict:
    blueprint = (
        {"max_attempts": 2}
        if mode == "training"
        else {"item_count": 1, "item_types": ["single_choice"], "difficulty_bands": []}
    )
    started = _call(
        dispatcher,
        LearningToolName.SESSION_START.value,
        _event(
            "learning.session.start",
            course_id=course_id,
            idempotency_key=f"start-{uuid4()}",
            payload={"course_revision": 1, "mode": mode, "blueprint": blueprint},
        ),
    )
    return started["result"]["session"]


def _preview(dispatcher, turn, payload, *, revision=None):
    return _call(
        dispatcher,
        "learning_task_preview",
        _event(
            "learning.task.preview",
            session_id=turn["session_id"],
            expected_revision=turn["session_revision"] if revision is None else revision,
            idempotency_key=f"preview-{uuid4()}",
            payload=payload,
        ),
    )


def _confirm(dispatcher, turn, vorbewertung_id, *, revision=None):
    return _call(
        dispatcher,
        "learning_task_confirm",
        _event(
            "learning.task.confirm",
            session_id=turn["session_id"],
            expected_revision=turn["session_revision"] if revision is None else revision,
            idempotency_key=f"confirm-{uuid4()}",
            payload={"vorbewertung_id": vorbewertung_id},
        ),
    )


def _setup(tmp_path: Path, **service_args):
    engine, factory, course_id = _store(tmp_path)
    service = AdaptiveSessionService(factory, **service_args)
    notes = NoteStore(factory)
    dispatcher = LearningDispatcher(
        gateways=build_session_gateways(service, notes), receipts=InMemoryReceiptStore()
    )
    return engine, dispatcher, course_id, notes


def test_preview_liefert_vorbewertung_ohne_revision(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    preview = _preview(dispatcher, turn, {"answer": {"choice_id": "b"}})
    assert preview["state"] == "completed", preview
    vor = preview["result"]["vorbewertung"]
    assert vor["quelle"] == "getippt" and vor["lesbar"] is True and vor["art"] == "deterministic"
    assert vor["feedback"]["state"] == "incorrect"
    assert preview["result"]["session"]["session_revision"] == turn["session_revision"]
    assert preview["result"]["session"]["task"]["id"] == turn["task"]["id"]
    assert preview["aggregate"]["revision"] == turn["session_revision"]
    engine.dispose()


def test_confirm_wie_answer(tmp_path: Path) -> None:
    engine, dispatcher, course_id, notes = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    vor = _preview(dispatcher, turn, {"answer": {"choice_id": "b"}})["result"]["vorbewertung"]
    confirmed = _confirm(dispatcher, turn, vor["id"])
    assert confirmed["state"] == "completed", confirmed
    session = confirmed["result"]["session"]
    assert session["session_revision"] == turn["session_revision"] + 2
    assert session["feedback"] is not None
    # a wrong training answer leaves a rating note, like task_answer
    assert len(notes.list_for("student-mcp", quelle="bewertung")) == 1
    engine.dispose()


def test_confirm_doppelt_session_state_conflict(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    vor = _preview(dispatcher, turn, {"answer": {"choice_id": "a"}})["result"]["vorbewertung"]
    first = _confirm(dispatcher, turn, vor["id"])
    assert first["state"] == "completed", first
    again = _confirm(
        dispatcher, turn, vor["id"], revision=first["result"]["session"]["session_revision"]
    )
    assert again["state"] == "rejected"
    assert again["error"]["code"] == "session_state_conflict"
    engine.dispose()


def test_preview_und_confirm_stale_revision(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    stale = _preview(dispatcher, turn, {"answer": {"choice_id": "a"}}, revision=99)
    assert stale["error"]["code"] == "session_revision_conflict"
    vor = _preview(dispatcher, turn, {"answer": {"choice_id": "a"}})["result"]["vorbewertung"]
    stale_confirm = _confirm(dispatcher, turn, vor["id"], revision=99)
    assert stale_confirm["error"]["code"] == "session_revision_conflict"
    engine.dispose()


@pytest.mark.parametrize(
    "payload",
    [
        {"handschrift_png_base64": "***not base64***"},
        {"handschrift_png_base64": base64.b64encode(b"GIF89a-not-a-png").decode()},
        {"handschrift_png_base64": "A" * 6_800_000},
        {"handschrift_png_base64": 5},
        {"handschrift_png_base64": PNG_B64, "answer": {"choice_id": "a"}},
        {},
        {"answer": {"choice_id": "a"}, "unbekannt": 1},
        {"answer": "kein objekt"},
        {"handschrift_png_base64": PNG_B64, "korrigiert_aus": str(uuid4())},
    ],
)
def test_preview_payload_streng(tmp_path: Path, payload: dict[str, object]) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    result = _preview(dispatcher, turn, payload)
    assert result["state"] == "rejected", result
    assert result["error"]["code"] == "invalid_session_request"
    engine.dispose()


def test_confirm_payload_streng_und_unbekannte_id(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    bad = _call(
        dispatcher,
        "learning_task_confirm",
        _event(
            "learning.task.confirm",
            session_id=turn["session_id"],
            expected_revision=turn["session_revision"],
            idempotency_key="confirm-bad",
            payload={"vorbewertung_id": str(uuid4()), "extra": 1},
        ),
    )
    assert bad["error"]["code"] == "invalid_session_request"
    unknown = _confirm(dispatcher, turn, str(uuid4()))
    assert unknown["error"]["code"] == "invalid_session_request"
    engine.dispose()


def test_preview_und_confirm_in_pruefung_verweigert(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id, mode="exam")
    preview = _preview(dispatcher, turn, {"answer": {"choice_id": "a"}})
    assert preview["error"]["code"] == "session_access_denied"
    confirm = _confirm(dispatcher, turn, str(uuid4()))
    assert confirm["error"]["code"] == "session_access_denied"
    engine.dispose()


def _bewertungsdaten(dispatcher, turn, *, revision=None, payload=None):
    return _call(
        dispatcher,
        "learning_task_bewertungsdaten",
        _event(
            "learning.task.grading_data",
            session_id=turn["session_id"],
            expected_revision=turn["session_revision"] if revision is None else revision,
            payload=payload or {},
        ),
    )


def test_bewertungsdaten_werkzeug_lesend_ohne_loesung_und_ohne_aenderung(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    result = _bewertungsdaten(dispatcher, turn)
    assert result["state"] == "completed"
    daten = result["result"]
    assert daten["art"] == "deterministic"
    assert daten["antwortformat"]["roh"] == {"auswahl": "list with exactly one label"}
    assert set(daten["antwortformat"]["etiketten"]["optionen"]) == {"A", "B"}
    assert daten["musterloesung"] is None and daten["kriterien"] == []
    assert "ui_intent" not in result
    # Lesend: Revision unveraendert, zweiter Aufruf mit derselben Revision gelingt.
    assert _bewertungsdaten(dispatcher, turn)["state"] == "completed"
    stale = _bewertungsdaten(dispatcher, turn, revision=turn["session_revision"] + 1)
    assert stale["error"]["code"] == "session_revision_conflict"
    bad = _bewertungsdaten(dispatcher, turn, payload={"answer": {}})
    assert bad["error"]["code"] == "invalid_session_request"
    engine.dispose()


def test_bewertungsdaten_werkzeug_in_pruefung_verweigert(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id, mode="exam")
    assert _bewertungsdaten(dispatcher, turn)["error"]["code"] == "session_access_denied"
    engine.dispose()


def test_preview_handschrift_ohne_leser_ist_nicht_verfuegbar(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    result = _preview(dispatcher, turn, {"handschrift_png_base64": PNG_B64})
    assert result["state"] == "unavailable"
    assert result["error"]["code"] == "session_grading_unavailable"
    engine.dispose()


def test_preview_externe_bewertung_fest_wird_uebernommen(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    # Die KI liest das Etikett, unter dem die Flaeche die richtige Option "a" zeigt.
    etikett = "AB"[[o["id"] for o in turn["task"]["options"]].index("a")]
    extern = {
        "grader": "ki_flaeche",
        "gelesen": {"lesbar": True, "text": etikett, "roh": {"auswahl": [etikett]}},
    }
    result = _preview(
        dispatcher, turn, {"handschrift_png_base64": PNG_B64, "externe_bewertung": extern}
    )
    assert result["state"] == "completed", result
    vor = result["result"]["vorbewertung"]
    assert vor["bewertet_von"] == "ki_flaeche" and vor["score"] == 1.0
    engine.dispose()


def test_preview_externe_bewertung_nicht_zuordenbar_wird_abgelehnt(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    extern = {
        "grader": "ki_flaeche",
        "gelesen": {"lesbar": True, "text": "Z", "roh": {"auswahl": ["Z"]}},
    }
    result = _preview(
        dispatcher, turn, {"handschrift_png_base64": PNG_B64, "externe_bewertung": extern}
    )
    # Abgelehnt (nicht "unavailable"): die Huelle wiederholt dann ohne externe Bewertung.
    assert result["state"] == "rejected", result
    assert result["error"]["code"] == "invalid_session_request"
    engine.dispose()


@pytest.mark.parametrize(
    "zusatz",
    [{"answer": {"choice_id": "a"}}, {"korrigiert_aus": str(uuid4())}, {}],
    ids=["mit_antwort", "mit_korrektur", "ohne_bild"],
)
def test_preview_externe_bewertung_nur_mit_bild(tmp_path: Path, zusatz) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    extern = {
        "grader": "ki_flaeche",
        "gelesen": {"lesbar": True, "text": "A", "roh": {"auswahl": ["A"]}},
    }
    result = _preview(dispatcher, turn, {"externe_bewertung": extern, **zusatz})
    assert result["state"] == "rejected", result
    assert result["error"]["code"] == "invalid_session_request"
    engine.dispose()


def test_server_verdrahtet_den_leser() -> None:
    import inspect

    assert "handschrift_leser=evaluation_client" in inspect.getsource(server)


# ---- feste Liste: learning_task_list und attempt_id -------------------------------------


def _drei_aufgaben(factory, course_id: str) -> None:
    from sqlalchemy import select

    with factory() as db, db.begin():
        concept_id = db.scalar(
            select(AdaptiveConcept.id).where(AdaptiveConcept.course_id == course_id)
        )
        for index in (2, 3):
            item_id = str(uuid4())
            db.add(
                AdaptiveItem(
                    id=item_id,
                    course_id=course_id,
                    course_revision=1,
                    activity_id=f"activity-{index}",
                    item_type="single_choice",
                    difficulty=0.3,
                    quality_status="approved",
                    expected_answer={"choice_id": "a"},
                    scoring_config={
                        "prompt": f"Frage {index}?",
                        "options": [
                            {"id": "a", "label": "Ja"},
                            {"id": "b", "label": "Nein"},
                        ],
                        "source_refs": [f"source://course/{index}"],
                        "hints": [],
                        "representation": "quiz",
                        "remediation_tags": [],
                    },
                    rubric=[],
                )
            )
            db.add(
                ItemConcept(
                    item_id=item_id,
                    concept_id=concept_id,
                    course_id=course_id,
                    course_revision=1,
                )
            )


def _liste_dispatcher(tmp_path: Path, notes: NoteStore | None = None):
    engine, factory, course_id = _store(tmp_path)
    _drei_aufgaben(factory, course_id)
    dispatcher = LearningDispatcher(
        gateways=build_session_gateways(AdaptiveSessionService(factory), notes),
        receipts=InMemoryReceiptStore(),
    )
    return engine, dispatcher, course_id, factory


def _liste_starten(dispatcher, course_id: str) -> dict:
    started = _call(
        dispatcher,
        LearningToolName.SESSION_START.value,
        _event(
            "learning.session.start",
            course_id=course_id,
            idempotency_key=f"start-{uuid4()}",
            payload={
                "course_revision": 1,
                "mode": "training",
                "blueprint": {"max_attempts": 3},
                "ablauf": "liste",
            },
        ),
    )
    assert started["state"] == "completed", started
    return started["result"]["session"]


def _task_list(dispatcher, session_id: str):
    return _call(
        dispatcher,
        "learning_task_list",
        _event("learning.task.list", session_id=session_id),
    )


def test_task_list_liefert_alle_aufgaben(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _liste_dispatcher(tmp_path)
    turn = _liste_starten(dispatcher, course_id)
    assert turn["ablauf"] == "liste"
    listed = _task_list(dispatcher, turn["session_id"])
    assert listed["state"] == "completed", listed
    assert listed["evidence"]["evidence_type"] == "application_readback"
    assert [e["nummer"] for e in listed["result"]["aufgaben"]] == [1, 2, 3]
    assert {e["status"] for e in listed["result"]["aufgaben"]} == {"offen"}
    assert listed["result"]["session"]["session_id"] == turn["session_id"]
    assert listed["result"]["session"]["ablauf"] == "liste"
    engine.dispose()


def test_preview_mit_attempt_id(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _liste_dispatcher(tmp_path)
    turn = _liste_starten(dispatcher, course_id)
    aufgaben = _task_list(dispatcher, turn["session_id"])["result"]["aufgaben"]
    zweiter = aufgaben[1]["attempt_id"]
    preview = _preview(
        dispatcher, turn, {"answer": {"choice_id": "a"}, "attempt_id": zweiter}
    )
    assert preview["state"] == "completed", preview
    assert preview["result"]["vorbewertung"]["attempt_id"] == zweiter
    confirmed = _call(
        dispatcher,
        "learning_task_confirm",
        _event(
            "learning.task.confirm",
            session_id=turn["session_id"],
            expected_revision=turn["session_revision"],
            idempotency_key=f"confirm-{uuid4()}",
            payload={
                "vorbewertung_id": preview["result"]["vorbewertung"]["id"],
                "attempt_id": zweiter,
            },
        ),
    )
    assert confirmed["state"] == "completed", confirmed
    nach = _task_list(dispatcher, turn["session_id"])["result"]["aufgaben"]
    assert [e["status"] for e in nach] == ["offen", "bestaetigt", "offen"]
    engine.dispose()


def test_answer_mit_attempt_id(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _liste_dispatcher(tmp_path)
    turn = _liste_starten(dispatcher, course_id)
    dritter = _task_list(dispatcher, turn["session_id"])["result"]["aufgaben"][2]["attempt_id"]
    answered = _call(
        dispatcher,
        "learning_task_answer",
        _event(
            "learning.task.answer",
            session_id=turn["session_id"],
            expected_revision=turn["session_revision"],
            idempotency_key=f"answer-{uuid4()}",
            payload={"answer": {"choice_id": "a"}, "attempt_id": dritter},
        ),
    )
    assert answered["state"] == "completed", answered
    nach = _task_list(dispatcher, turn["session_id"])["result"]["aufgaben"]
    assert [e["status"] for e in nach] == ["offen", "offen", "bestaetigt"]
    engine.dispose()


def test_task_list_bei_anpassend_abgelehnt(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    assert turn["ablauf"] == "anpassend"
    listed = _task_list(dispatcher, turn["session_id"])
    assert listed["state"] == "rejected"
    assert listed["error"]["code"] == "invalid_session_request"
    engine.dispose()


@pytest.mark.parametrize("ablauf", ["kreuz", 5, True])
def test_start_ablauf_streng(tmp_path: Path, ablauf: object) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    started = _call(
        dispatcher,
        LearningToolName.SESSION_START.value,
        _event(
            "learning.session.start",
            course_id=course_id,
            idempotency_key=f"start-{uuid4()}",
            payload={
                "course_revision": 1,
                "mode": "training",
                "blueprint": {"max_attempts": 2},
                "ablauf": ablauf,
            },
        ),
    )
    assert started["state"] == "rejected", started
    assert started["error"]["code"] == "invalid_session_request"
    engine.dispose()


def test_attempt_id_muss_text_sein(tmp_path: Path) -> None:
    engine, dispatcher, course_id, _ = _setup(tmp_path)
    turn = _start(dispatcher, course_id)
    result = _preview(dispatcher, turn, {"answer": {"choice_id": "a"}, "attempt_id": 5})
    assert result["state"] == "rejected", result
    assert result["error"]["code"] == "invalid_session_request"
    engine.dispose()


def test_notiz_gehoert_zur_aufgabe_des_versuchs(tmp_path: Path) -> None:
    engine, factory, course_id = _store(tmp_path)
    _drei_aufgaben(factory, course_id)
    notes = NoteStore(factory)
    dispatcher = LearningDispatcher(
        gateways=build_session_gateways(AdaptiveSessionService(factory), notes),
        receipts=InMemoryReceiptStore(),
    )
    turn = _liste_starten(dispatcher, course_id)
    aufgaben = _task_list(dispatcher, turn["session_id"])["result"]["aufgaben"]
    dritte = aufgaben[2]
    answered = _call(
        dispatcher,
        "learning_task_answer",
        _event(
            "learning.task.answer",
            session_id=turn["session_id"],
            expected_revision=turn["session_revision"],
            idempotency_key=f"answer-{uuid4()}",
            payload={"answer": {"choice_id": "b"}, "attempt_id": dritte["attempt_id"]},
        ),
    )
    assert answered["state"] == "completed", answered
    gespeichert = notes.list_for("student-mcp", quelle="bewertung")
    assert len(gespeichert) == 1
    assert gespeichert[0]["anchor"] == {"art": "aufgabe", "aufgabe": dritte["task"]["id"]}
    assert "Frage 3" in gespeichert[0]["selection"]
    engine.dispose()
