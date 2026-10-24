"""Learning's own canvas: open creates the launch, save, submit, evaluation sees image + text.

The PenEcho learning bridge is lost; Learning serves the canvas behind the same
contract (bridge/local_canvas.py). Practice items cite canonical JSON source refs.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.bridge.dispatcher import InMemoryReceiptStore, LearningDispatcher
from spaces.learning.bridge.local_canvas import LocalCanvasBackend, LocalCanvasLauncher
from spaces.learning.contracts.events import LearningEventType, LearningToolName
from spaces.learning.contracts.mcp_models import ActorV1, ConfirmationV1, EventEnvelopeV1, ToolRequestV1
from spaces.learning.contracts.penecho import CanvasDocumentV1, CanvasObjectV1
from spaces.learning.mcp.tools.canvas import (
    CanvasAdaptiveSubmissionService, CanvasGateway, build_canvas_gateways,
)
from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept, AdaptiveItem, ItemConcept, LearningEvaluation,
)
from spaces.learning.services.adaptive_engine.repository import SessionInput
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.course_factory.model_gateway import GatewayResult
from spaces.learning.services.db.models import Base
from spaces.learning.services.evaluation.canvas_artifacts import CanvasArtifactStore
from spaces.learning.services.evaluation.schemas import CriterionEvaluation, RubricModelOutput

PNG = b"\x89PNG\r\n\x1a\nlocal-canvas"
ORIGIN = "http://127.0.0.1:8139"
SOURCE, CHUNK = str(uuid4()), str(uuid4())
REF = json.dumps({
    "chunk_id": CHUNK, "content": "Das Werkstueck wird mit Zwingen gesichert.",
    "locator": {"heading": "Waehrend der Arbeit"}, "source_id": SOURCE, "source_revision": 2,
}, sort_keys=True)


class _Gateway:
    def __init__(self) -> None:
        self.invocations = []

    async def generate(self, invocation, output_model):
        self.invocations.append(invocation)
        return GatewayResult(
            output=output_model.model_validate(RubricModelOutput(
                schema_version="rubric-evaluation-v1",
                criteria=[CriterionEvaluation(
                    criterion_id="zwinge", awarded_points=1,
                    feedback="Die Zwinge haelt das Werkstueck.", source_refs=["S1"],
                )],
                confidence=0.9, misconception_tags=[],
            )),
            evidence_ref="openfang://completion/local-canvas-1",
        )


class _Tutor:
    def __init__(self) -> None:
        self.calls = []

    async def ask(self, **kwargs):
        self.calls.append(kwargs)
        return {"answer": "Schau auf die Befestigung.", "source_refs": [REF], "next_step": "."}


def _event(event_type, *, session_id, course_id, payload, expected_revision=None,
           key=None, confirmation=None, actor="student-1"):
    return EventEnvelopeV1(
        event_type=event_type, invocation_id=uuid4(), correlation_id=uuid4(),
        actor=ActorV1(actor_id=actor, actor_type="local_user"),
        course_id=course_id, session_id=session_id, payload=payload,
        expected_revision=expected_revision, idempotency_key=key, confirmation=confirmation,
    )


@pytest.fixture()
def world(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'local-canvas.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, connection_record) -> None:
        del connection_record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    course_id, concept_id, canvas_item = uuid4(), uuid4(), UUID(int=1)
    with factory() as db, db.begin():
        db.add(AdaptiveConcept(
            id=str(concept_id), course_id=str(course_id), course_revision=1,
            concept_key="werkstueck-sichern", title="Werkstueck sichern",
        ))
        db.add(AdaptiveItem(
            id=str(canvas_item), course_id=str(course_id), course_revision=1,
            activity_id="k4-zeichnung", item_type="penecho_canvas", difficulty=0.5,
            quality_status="approved", expected_answer={"text": "Werkstueck in Zwinge."},
            scoring_config={"prompt": "Zeichne das gesicherte Werkstueck.", "options": [],
                            "source_refs": [REF], "hints": [], "presentation": {}},
            rubric=[{"criterion_id": "zwinge", "description": "Zwinge sichtbar", "points": 1}],
        ))
        db.add(ItemConcept(item_id=str(canvas_item), concept_id=str(concept_id),
                           course_id=str(course_id), course_revision=1))
    started = AdaptiveSessionService(factory, external_rubric_types=frozenset({"penecho_canvas"})).start(
        SessionInput(course_id=str(course_id), course_revision=1, actor_id="student-1",
                     mode="training", blueprint={"max_attempts": 1})
    )
    backend = LocalCanvasBackend(root=tmp_path / "canvas-sessions", origin=ORIGIN)
    (tmp_path / "artifacts").mkdir()
    model = _Gateway()
    tutor = _Tutor()
    gateway = CanvasGateway(
        penecho=backend,
        artifacts=CanvasArtifactStore(artifact_root=tmp_path / "artifacts"),
        evaluations=CanvasAdaptiveSubmissionService(session_factory=factory, gateway=model),
        tutor=tutor,
        launcher=LocalCanvasLauncher(session_factory=factory, backend=backend),
    )
    dispatcher = LearningDispatcher(gateways=build_canvas_gateways(gateway),
                                    receipts=InMemoryReceiptStore())
    backend.tutor = tutor  # test hook: the canvas hint test reads what the tutor got
    yield dispatcher, started, course_id, canvas_item, model, factory, backend
    engine.dispose()


def test_open_creates_the_launch_and_a_drawing_is_evaluated(world) -> None:
    dispatcher, started, course_id, canvas_item, model, factory, backend = world
    sid = UUID(started.session_id)
    common = {"session_ref": "zeichnen-1"}

    def call(tool, **kw):
        return dispatcher.dispatch(ToolRequestV1(tool=tool, event=_event(
            kw.pop("event_type"), session_id=sid, course_id=course_id, **kw)))

    opened = call(LearningToolName.CANVAS_OPEN, event_type=LearningEventType.CANVAS_OPEN,
                  payload={**common, "activity_id": str(canvas_item)})
    assert opened.state == "completed"
    launch = backend.open_session(sid, "zeichnen-1").launch
    assert launch.source_refs[0].locator == f"source://{SOURCE}/2/{CHUNK}"
    assert launch.help_policy.max_hints == 2

    document = CanvasDocumentV1.model_validate(opened.result["canvas"]["document"])
    drawn = document.model_copy(update={"objects": (CanvasObjectV1(
        object_id="strich-1", object_type="path", x=10, y=10, width=200, height=80, rotation=0,
    ),)})
    saved = call(LearningToolName.CANVAS_SAVE, event_type=LearningEventType.CANVAS_SAVE,
                 expected_revision=0, key="save-1",
                 payload={**common, "document": drawn.model_dump(mode="json"),
                          "snapshot_png_base64": base64.b64encode(PNG).decode()})
    assert saved.state == "completed" and saved.aggregate.revision == 1

    submitted = call(LearningToolName.CANVAS_SUBMIT, event_type=LearningEventType.CANVAS_SUBMIT,
                     expected_revision=1, key="submit-1",
                     confirmation=ConfirmationV1(confirmed=True, approval_ref="zeichnung-1"),
                     payload={**common, "submission_id": str(uuid4()),
                              "adaptive_session_revision": started.session_revision})
    assert submitted.state == "completed", submitted
    assert submitted.result["evaluation"]["score"] == 1.0
    invocation = model.invocations[0]
    assert invocation.image_inputs[0].content == PNG
    assert invocation.input_payload["sources"][0]["source"] == REF
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(LearningEvaluation)) == 1


def test_canvas_hint_accepts_the_tutors_practice_source_refs(world) -> None:
    # The tutor cites the practice item's JSON refs; the launch carries source://
    # locators. Comparing the strings refused every hint as "not source grounded".
    dispatcher, started, course_id, canvas_item, model, factory, backend = world
    sid = UUID(started.session_id)
    common = {"session_ref": "zeichnen-1"}

    def call(tool, **kw):
        return dispatcher.dispatch(ToolRequestV1(tool=tool, event=_event(
            kw.pop("event_type"), session_id=sid, course_id=course_id, **kw)))

    opened = call(LearningToolName.CANVAS_OPEN, event_type=LearningEventType.CANVAS_OPEN,
                  payload={**common, "activity_id": str(canvas_item)})
    document = CanvasDocumentV1.model_validate(opened.result["canvas"]["document"])
    drawn = document.model_copy(update={"objects": (CanvasObjectV1(
        object_id="strich-1", object_type="path", x=1, y=1, width=5, height=5, rotation=0,
    ),)})
    call(LearningToolName.CANVAS_SAVE, event_type=LearningEventType.CANVAS_SAVE,
         expected_revision=0, key="save-h",
         payload={**common, "document": drawn.model_dump(mode="json"),
                  "snapshot_png_base64": base64.b64encode(PNG).decode()})

    hinted = call(LearningToolName.CANVAS_HINT, event_type=LearningEventType.CANVAS_HINT,
                  expected_revision=1,
                  payload={**common, "selection": {"object_ids": ["strich-1"]}})

    assert hinted.state == "completed", hinted
    assert hinted.result["hint"]["answer"] == "Schau auf die Befestigung."
    # The tutor gets the saved drawing, not only the selected stroke ids.
    assert base64.b64decode(backend.tutor.calls[0]["drawing_png_base64"]) == PNG


def test_submission_service_builds_a_fresh_gateway_per_drawing() -> None:
    # asyncio.run per submission closes its loop; a gateway built once with a pooled
    # async client failed from the second call ("Event loop is closed", seen live in
    # the worker and the rubric evaluator). A factory gives each drawing its own.
    built: list[_Gateway] = []

    def factory() -> _Gateway:
        built.append(_Gateway())
        return built[-1]

    service = CanvasAdaptiveSubmissionService(session_factory=lambda: None, gateway=factory)

    assert service._model_gateway() is built[0]
    assert service._model_gateway() is built[1]
    fixed = _Gateway()
    assert CanvasAdaptiveSubmissionService(
        session_factory=lambda: None, gateway=fixed
    )._model_gateway() is fixed


def test_another_person_cannot_open_the_canvas(world) -> None:
    dispatcher, started, course_id, canvas_item, *_ = world
    outcome = dispatcher.dispatch(ToolRequestV1(tool=LearningToolName.CANVAS_OPEN, event=_event(
        LearningEventType.CANVAS_OPEN, session_id=UUID(started.session_id), course_id=course_id,
        payload={"session_ref": "zeichnen-1", "activity_id": str(canvas_item)}, actor="mallory",
    )))
    assert outcome.state != "completed"
