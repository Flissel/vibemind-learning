from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from spaces.learning.bridge.dispatcher import InMemoryReceiptStore, LearningDispatcher
from spaces.learning.contracts.events import LearningToolName
from spaces.learning.contracts.mcp_models import ToolRequestV1
from spaces.learning.mcp.tools.sessions import (
    AdaptiveSessionGateway,
    build_session_gateways,
)
from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ConceptMastery,
    ItemConcept,
)
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    SessionStartCommand,
)
from spaces.learning.services.db.models import Base

ACTOR = "lh-1-test"


def _build(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'resume.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, connection_record) -> None:
        del connection_record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    course_id = str(uuid4())
    concepts: dict[str, str] = {}

    def add_item(db, revision: int, key: str, title: str, status: str) -> None:
        concept_id = str(uuid4())
        item_id = str(uuid4())
        concepts[f"{revision}:{key}"] = concept_id
        db.add(
            AdaptiveConcept(
                id=concept_id,
                course_id=course_id,
                course_revision=revision,
                concept_key=key,
                title=title,
            )
        )
        db.add(
            AdaptiveItem(
                id=item_id,
                course_id=course_id,
                course_revision=revision,
                activity_id=f"activity-{revision}-{key}",
                item_type="single_choice",
                difficulty=0.3,
                quality_status=status,
                expected_answer={"choice_id": "a"},
                scoring_config={
                    "prompt": "Frage?",
                    "options": [
                        {"id": "a", "label": "Ja"},
                        {"id": "b", "label": "Nein"},
                    ],
                    "source_refs": ["source://course/1"],
                    "hints": ["Hinweis."],
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
                course_revision=revision,
            )
        )

    with factory() as db, db.begin():
        add_item(db, 1, "alt", "Alt", "approved")
        # Revision 2 ist die hoechste freigegebene; Revision 3 ist nur im Review.
        add_item(db, 2, "beta", "Beta", "approved")
        add_item(db, 2, "alpha", "Alpha", "approved")
        add_item(db, 3, "entwurf", "Entwurf", "review")
    return engine, factory, course_id, concepts


def _start(service: AdaptiveSessionService, course_id: str, revision: int):
    return service.start(
        SessionStartCommand(
            course_id=course_id,
            course_revision=revision,
            actor_id=ACTOR,
            mode="training",
            blueprint={"max_attempts": 5},
        )
    )


def _request(
    tool: LearningToolName,
    event_type: str,
    course_id: str,
    payload: dict[str, object],
) -> ToolRequestV1:
    return ToolRequestV1.model_validate(
        {
            "tool": tool.value,
            "event": {
                "event_type": event_type,
                "invocation_id": str(uuid4()),
                "correlation_id": str(uuid4()),
                "actor": {"actor_id": ACTOR, "actor_type": "local_user"},
                "course_id": course_id,
                "payload": payload,
            },
        }
    )


def _resume_request(course_id: str, revision: int) -> ToolRequestV1:
    return _request(
        LearningToolName.SESSION_RESUME,
        "learning.session.resume",
        course_id,
        {"course_revision": revision},
    )


def test_ueberblick_nimmt_hoechste_freigegebene_revision(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _build(tmp_path)
    result = AdaptiveSessionService(factory).ueberblick(
        course_id=course_id, actor_id=ACTOR
    )
    assert result["course_revision"] == 2
    assert [b["titel"] for b in result["begriffe"]] == ["Alpha", "Beta"]
    engine.dispose()


def test_ueberblick_ohne_items_hat_keine_revision(tmp_path: Path) -> None:
    engine, factory, _, _ = _build(tmp_path)
    result = AdaptiveSessionService(factory).ueberblick(
        course_id=str(uuid4()), actor_id=ACTOR
    )
    assert result["course_revision"] is None
    assert result["begriffe"] == []
    assert result["session"] is None
    engine.dispose()


def test_ueberblick_beherrschung_je_begriff(tmp_path: Path) -> None:
    engine, factory, course_id, concepts = _build(tmp_path)
    with factory() as db, db.begin():
        db.add(
            ConceptMastery(
                actor_id=ACTOR,
                concept_id=concepts["2:alpha"],
                alpha=3.0,
                beta=1.0,
                revision=1,
            )
        )
        db.add(
            ConceptMastery(
                actor_id="lh-2-test",
                concept_id=concepts["2:beta"],
                alpha=1.0,
                beta=1.0,
                revision=1,
            )
        )
    result = AdaptiveSessionService(factory).ueberblick(
        course_id=course_id, actor_id=ACTOR
    )
    by_key = {b["concept_key"]: b["beherrschung"] for b in result["begriffe"]}
    assert by_key == {"alpha": 0.75, "beta": None}
    engine.dispose()


def test_ueberblick_liefert_aktive_sitzung_der_person(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _build(tmp_path)
    service = AdaptiveSessionService(factory)
    started = _start(service, course_id, 2)
    mine = service.ueberblick(course_id=course_id, actor_id=ACTOR)
    assert mine["session"]["session_id"] == started.session_id
    other = service.ueberblick(course_id=course_id, actor_id="lh-2-test")
    assert other["session"] is None
    engine.dispose()


def test_gateway_resume_liefert_none_ohne_aktive_sitzung(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _build(tmp_path)
    gateway = AdaptiveSessionGateway(AdaptiveSessionService(factory))
    outcome = gateway.execute(_resume_request(course_id, 2))
    assert outcome.state == "completed"
    assert outcome.result == {"session": None}
    engine.dispose()


def test_gateway_resume_setzt_fort_statt_neu_zu_starten(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _build(tmp_path)
    service = AdaptiveSessionService(factory)
    started = _start(service, course_id, 2)
    outcome = AdaptiveSessionGateway(service).execute(_resume_request(course_id, 2))
    session = outcome.result["session"]
    assert session["session_id"] == started.session_id
    assert session["session_revision"] == started.session_revision
    engine.dispose()


def test_resume_ist_lesend_ohne_idempotenzschluessel(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _build(tmp_path)
    service = AdaptiveSessionService(factory)
    dispatcher = LearningDispatcher(
        gateways=build_session_gateways(service), receipts=InMemoryReceiptStore()
    )
    result = dispatcher.dispatch(_resume_request(course_id, 2))
    assert result.state == "completed"
    assert result.result == {"session": None}

    started = _start(service, course_id, 2)
    result = dispatcher.dispatch(_resume_request(course_id, 2))
    assert result.state == "completed"
    assert result.result["session"]["session_id"] == started.session_id

    overview = dispatcher.dispatch(
        _request(
            LearningToolName.COURSE_OVERVIEW,
            "learning.course.overview",
            course_id,
            {},
        )
    )
    assert overview.state == "completed"
    assert overview.result["course_revision"] == 2
