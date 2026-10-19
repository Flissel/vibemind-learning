from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.bridge.dispatcher import ApplicationOutcomeV1, LearningDispatcher
from spaces.learning.bridge.penecho_client import PenEchoExportV1, PenEchoSessionV1
from spaces.learning.bridge.ui_bridge import UiDeliveryReceipt, UiDeliveryResult
from spaces.learning.contracts.events import LearningEventType, LearningToolName
from spaces.learning.contracts.mcp_models import ActorV1, ConfirmationV1, EventEnvelopeV1
from spaces.learning.contracts.outcomes import AggregateRefV1, EvidenceRefV1, TruthReadbackV1
from spaces.learning.contracts.penecho import (
    CanvasArtifactV1,
    CanvasDocumentV1,
    CanvasHelpPolicyV1,
    CanvasObjectV1,
    CanvasSaveV1,
    PenEchoLaunchContextV1,
    SourceReferenceV1,
)
from spaces.learning.deployment import runtime_api
from spaces.learning.deployment.course_factory_routes import (
    CourseFactoryRuntime,
    get_course_factory_runtime,
)
from spaces.learning.deployment.vibemind_auth import SERVICE_KEY_ENV
from spaces.learning.mcp import server
from spaces.learning.mcp.tools.canvas import (
    CanvasAdaptiveSubmissionService,
    CanvasGateway,
    build_canvas_gateways,
)
from spaces.learning.mcp.tools.generation import build_generation_gateways
from spaces.learning.services.course_factory.application import CourseFactoryApplication
from spaces.learning.mcp.tools.materials import MaterialImportGateway
from spaces.learning.mcp.tools.navigation import build_navigation_gateways
from spaces.learning.mcp.tools.sessions import build_session_gateways
from spaces.learning.mcp.tools.status import StructuralStatusGateway
from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ConceptMastery,
    ItemConcept,
    LearningEvaluation,
    MasteryEvidence,
)
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.course_factory.artifact_store import CourseFactoryArtifactStore
from spaces.learning.services.course_factory.catalog import SourceCatalogLoader
from spaces.learning.services.course_factory.model_gateway import GatewayResult
from spaces.learning.services.course_factory.publisher import (
    CourseDraftPublisher,
    DraftDeliveryReceipt,
    LearnHouseCourseReadback,
)
from spaces.learning.services.course_factory.quality_gate import QualityGate
from spaces.learning.services.course_factory.repository import CourseFactoryRepository
from spaces.learning.services.course_factory.roles.schemas import (
    ArchitectOutput,
    AssessmentOutput,
    ConceptMapOutput,
    LessonOutput,
    QualityReviewOutput,
    SourceVerificationOutput,
)
from spaces.learning.services.course_factory.runner import CourseFactoryRunner
from spaces.learning.services.course_factory.schemas import (
    ActivityDraft,
    ChapterDraft,
    Citation,
    Claim,
    ConceptDraft,
    CourseDraft,
    LessonDraft,
    RubricCriterion,
    SourceProvenanceRef,
)
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.course_factory.team import CourseAgentTeam
from spaces.learning.services.db.models import (
    Base,
    LearningInvocationReceipt,
)
from spaces.learning.services.db.repository import SqlReceiptStore
from spaces.learning.services.evaluation.canvas_artifacts import (
    CanvasArtifactStore,
    encode_canvas_document,
)
from spaces.learning.services.evaluation.schemas import CriterionEvaluation, RubricModelOutput
from spaces.learning.services.ingestion.models import LearningSourceChunk, LearningSourceRevision
from spaces.learning.services.ingestion.material_staging import MaterialStagingService
from spaces.learning.services.ingestion.outbox_worker import QdrantOutboxWorker
from spaces.learning.services.ingestion.pipeline import IngestionPipeline
from spaces.learning.services.ingestion.qdrant_index import QdrantHit, QdrantPoint
from spaces.learning.services.ingestion.repository import SourceRepository


CORRELATION_ID = UUID("11111111-1111-4111-8111-111111111111")
COURSE_ID = UUID("22222222-2222-4222-8222-222222222222")
SOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
CHAPTER_ID = UUID("44444444-4444-4444-8444-444444444444")
ACTOR = ActorV1(actor_id="golden-student", actor_type="local_user")
PNG = b"\x89PNG\r\n\x1a\nvibemind-learning-golden-path"


class _Index:
    def __init__(self) -> None:
        self.points: dict[str, QdrantPoint] = {}

    def ensure_schema(self) -> None:
        return None

    def upsert(self, points: list[QdrantPoint]) -> None:
        self.points.update({point.point_id: point for point in points})

    def tombstone(self, point_ids, **kwargs) -> None:
        del kwargs
        for point_id in point_ids:
            self.points.pop(point_id, None)

    def retrieve(self, point_ids: list[str]) -> tuple[QdrantHit, ...]:
        return tuple(
            QdrantHit(point_id=value, score=1.0, payload=self.points[value].payload)
            for value in point_ids
            if value in self.points
        )


class _Embedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0, 0.5] for text in texts]


class _UiRecorder:
    def __init__(self) -> None:
        self.intents: list[dict[str, Any]] = []

    def try_deliver(self, intent, *, correlation_id: UUID) -> UiDeliveryResult:
        assert correlation_id == CORRELATION_ID
        value = intent.model_dump(mode="json")
        self.intents.append(value)
        return UiDeliveryResult(
            delivered=True,
            receipt=UiDeliveryReceipt(
                accepted=True,
                aggregate_revision=int(value["aggregate_revision"]),
                event_id=f"golden-ui-{len(self.intents)}",
            ),
        )


class _LearnHouse:
    def __init__(self) -> None:
        self.course_revision = 1
        self.review_status = "draft"
        self.published = False

    def execute(self, request) -> ApplicationOutcomeV1:
        if request.tool is LearningToolName.MATERIAL_IMPORT:
            return ApplicationOutcomeV1(
                state="completed",
                aggregate=AggregateRefV1(
                    aggregate_type="source", aggregate_id=str(SOURCE_ID), revision=2
                ),
                result={"source_id": str(SOURCE_ID), "revision": 2},
            )
        if request.tool is LearningToolName.CHAPTER_OPEN:
            return ApplicationOutcomeV1(
                state="completed",
                aggregate=AggregateRefV1(
                    aggregate_type="chapter",
                    aggregate_id=str(CHAPTER_ID),
                    revision=self.course_revision,
                ),
                result={"chapter_id": str(CHAPTER_ID), "title": "Authority boundaries"},
            )
        raise ValueError("unsupported LearnHouse golden-path operation")

    def readback(self, request, outcome) -> TruthReadbackV1:
        assert outcome.aggregate is not None
        return TruthReadbackV1(
            invocation_id=request.event.invocation_id,
            correlation_id=request.event.correlation_id,
            owner="learnhouse",
            terminal_state="completed",
            aggregate=outcome.aggregate,
            evidence=EvidenceRefV1(
                owner="learnhouse",
                evidence_id=f"learnhouse:{outcome.aggregate.aggregate_id}:{outcome.aggregate.revision}",
                evidence_type="application_readback",
            ),
        )

    async def stage_draft(self, **values) -> DraftDeliveryReceipt:
        assert values["expected_revision"] == self.course_revision
        self.course_revision += 1
        return DraftDeliveryReceipt(
            course_id=values["course_id"],
            factory_job_id=values["draft"].job_id,
            attempt_number=values["draft"].attempt_number,
            draft_hash=values["draft_hash"],
            learnhouse_revision=self.course_revision,
            evidence_ref=f"learnhouse://courses/{COURSE_ID}/revision/{self.course_revision}",
        )

    async def approve(self, **values) -> LearnHouseCourseReadback:
        assert values["expected_revision"] == self.course_revision
        self.course_revision += 1
        self.review_status = "approved"
        return self._course_readback()

    async def publish(self, **values) -> LearnHouseCourseReadback:
        assert values["expected_revision"] == self.course_revision
        self.course_revision += 1
        self.published = True
        return self._course_readback()

    async def read_course(self, **values) -> LearnHouseCourseReadback:
        assert values["course_id"] == str(COURSE_ID)
        return self._course_readback()

    def _course_readback(self) -> LearnHouseCourseReadback:
        return LearnHouseCourseReadback(
            course_id=str(COURSE_ID),
            revision=self.course_revision,
            review_status=self.review_status,
            published=self.published,
        )


class _GenerationGateway:
    def __init__(self) -> None:
        self.draft: CourseDraft | None = None
        self.roles: list[str] = []

    async def generate(self, invocation, output_model):
        self.roles.append(invocation.role)
        values: dict[str, object] = {
            "architect": ArchitectOutput(
                schema_version="architect-v1",
                audience="AI course students",
                prerequisites=[],
                outcomes=["Operate an authorized AI workflow"],
                chapters=["Authority boundaries"],
            ),
            "concept_mapper": ConceptMapOutput(
                schema_version="concept-map-v1",
                concepts=["authority"],
                dependencies=[],
                chapter_coverage={"Authority boundaries": ["authority"]},
            ),
            "lesson_author": LessonOutput(
                schema_version="lesson-v1",
                lessons=[{
                    "chapter": "Authority boundaries",
                    "title": "Authorized execution",
                    "body": "Provider execution stays behind OpenFang.",
                }],
            ),
            "assessment_designer": AssessmentOutput(
                schema_version="assessment-v1",
                activities=[{
                    "concept": "authority",
                    "type": "penecho_canvas",
                    "prompt": "Visualize the authority chain.",
                }],
            ),
            "quality_reviewer": QualityReviewOutput(
                schema_version="quality-review-v1", decision="pass", issues=[], score=0.95
            ),
        }
        if invocation.role == "source_verifier":
            assert self.draft is not None
            values["source_verifier"] = SourceVerificationOutput(
                schema_version="source-verification-v2",
                draft=self.draft,
                unsupported_claim_ids=[],
            )
        output = values[invocation.role]
        assert isinstance(output, output_model)
        return GatewayResult(
            output=output,
            evidence_ref=f"openfang://deterministic/{invocation.role}",
        )


class _CanvasGateway:
    async def generate(self, invocation, output_model):
        assert invocation.image_inputs[0].content == PNG
        output = RubricModelOutput(
            schema_version="rubric-evaluation-v1",
            criteria=[CriterionEvaluation(
                criterion_id="authority",
                awarded_points=1,
                feedback="The OpenFang authority boundary and readback are connected.",
                source_refs=[f"source://{SOURCE_ID}/chunks/authority"],
            )],
            confidence=0.95,
            misconception_tags=[],
        )
        return GatewayResult(
            output=output_model.model_validate(output),
            evidence_ref="openfang://deterministic/canvas-rubric",
        )


class _Tutor:
    async def ask(self, **values):
        del values
        return {
            "answer": "Connect the authority boundary to terminal readback.",
            "source_refs": [f"source://{SOURCE_ID}/chunks/authority"],
            "next_step": "Mark OpenFang as the authority boundary.",
        }


class _PenEcho:
    def __init__(self) -> None:
        self.launch: PenEchoLaunchContextV1 | None = None
        self.document: CanvasDocumentV1 | None = None
        self.revision = 0

    def register(self, launch: PenEchoLaunchContextV1) -> None:
        self.launch = launch
        self.document = CanvasDocumentV1(
            project_id=uuid4(), canvas_id=uuid4(), revision=0, background="white", objects=()
        )

    def open_session(self, session_id, session_ref):
        assert self.launch is not None and self.document is not None
        assert session_id == self.launch.session_id
        assert session_ref == self.launch.bridge_session_ref
        return PenEchoSessionV1(
            id=session_id,
            revision=self.revision,
            launch=self.launch,
            document=self.document,
            artifacts=(),
        )

    def save_session(self, session_id, session_ref, save: CanvasSaveV1, *, snapshot_png_base64):
        assert base64.b64decode(snapshot_png_base64) == PNG
        assert save.expected_revision == self.revision
        self.revision += 1
        self.document = save.document.model_copy(update={"revision": self.revision})
        return self.open_session(session_id, session_ref)

    def export_session(self, session_id, session_ref):
        current = self.open_session(session_id, session_ref)
        assert current.document is not None
        structured = encode_canvas_document(current.document)
        return PenEchoExportV1(
            id=session_id,
            revision=self.revision,
            document=current.document,
            artifacts=(
                CanvasArtifactV1(
                    artifact_id=uuid5(session_id, "structured"),
                    artifact_kind="structured_canvas",
                    media_type="application/json",
                    sha256=hashlib.sha256(structured).hexdigest(),
                    size_bytes=len(structured),
                    locator="artifact://penecho/structured.json",
                    canvas_revision=self.revision,
                ),
                CanvasArtifactV1(
                    artifact_id=uuid5(session_id, "snapshot"),
                    artifact_kind="rendered_snapshot",
                    media_type="image/png",
                    sha256=hashlib.sha256(PNG).hexdigest(),
                    size_bytes=len(PNG),
                    locator="artifact://penecho/snapshot.png",
                    canvas_revision=self.revision,
                ),
            ),
            snapshot_png_base64=base64.b64encode(PNG).decode(),
        )


def _draft(job_id: str, source_revision: LearningSourceRevision, chunk: LearningSourceChunk) -> CourseDraft:
    citation_id = str(uuid5(UUID(job_id), "citation"))
    return CourseDraft(
        schema_version="course-draft-v1",
        job_id=job_id,
        attempt_number=1,
        title="Authorized AI Operations",
        outcomes=["Operate an authorized AI workflow"],
        source_provenance=[SourceProvenanceRef(
            source_id=str(SOURCE_ID), revision=1, content_hash=source_revision.content_hash
        )],
        citations=[Citation(
            citation_id=citation_id,
            source_id=str(SOURCE_ID),
            source_revision=1,
            chunk_id=chunk.id,
            content_hash=chunk.content_hash,
            locator=chunk.locator,
        )],
        concepts=[ConceptDraft(concept_id="authority", title="Authority boundary")],
        chapters=[ChapterDraft(
            chapter_id="authority-boundaries",
            title="Authority boundaries",
            lesson_ids=["authorized-execution"],
            activity_ids=["authority-canvas"],
        )],
        lessons=[LessonDraft(
            lesson_id="authorized-execution",
            chapter_id="authority-boundaries",
            title="Authorized execution",
            body="Provider execution stays behind OpenFang.",
            concept_ids=["authority"],
            claims=[Claim(
                claim_id="openfang-authority",
                text="Provider execution stays behind OpenFang.",
                citation_ids=[citation_id],
            )],
        )],
        activities=[ActivityDraft(
            activity_id="authority-canvas",
            chapter_id="authority-boundaries",
            type="penecho_canvas",
            concept_ids=["authority"],
            prompt="Visualize the OpenFang authority chain and terminal readback.",
            expected_answer="OpenFang authorizes provider execution and readback proves completion.",
            citation_ids=[citation_id],
            rubric=[RubricCriterion(
                criterion_id="authority",
                description="Shows authority and terminal readback",
                points=1,
            )],
        )],
    )


def _seed_adaptive(factory, *, course_revision: int, source_locator: str) -> tuple[str, str]:
    concept_id = str(uuid5(COURSE_ID, "authority-concept"))
    quiz_id = str(uuid5(COURSE_ID, "intro-quiz"))
    canvas_id = str(uuid5(COURSE_ID, "authority-canvas"))
    final_id = str(uuid5(COURSE_ID, "final-quiz"))
    with factory() as db, db.begin():
        db.add(AdaptiveConcept(
            id=concept_id,
            course_id=str(COURSE_ID),
            course_revision=course_revision,
            concept_key="authority.boundary",
            title="Authority boundary",
        ))
        items = (
            AdaptiveItem(
                id=quiz_id,
                course_id=str(COURSE_ID),
                course_revision=course_revision,
                activity_id="intro-quiz",
                item_type="single_choice",
                difficulty=0.25,
                quality_status="approved",
                expected_answer={"choice_id": "openfang"},
                scoring_config={
                    "prompt": "Which component authorizes provider execution?",
                    "options": [
                        {"id": "openfang", "label": "OpenFang"},
                        {"id": "renderer", "label": "Electron renderer"},
                    ],
                    "source_refs": [source_locator],
                    "hints": ["Follow the admitted authority chain."],
                    "representation": "quiz",
                    "remediation_tags": [],
                },
                rubric=[],
            ),
            AdaptiveItem(
                id=canvas_id,
                course_id=str(COURSE_ID),
                course_revision=course_revision,
                activity_id="authority-canvas",
                item_type="penecho_canvas",
                difficulty=0.55,
                quality_status="approved",
                expected_answer={"text": "Show OpenFang authority and terminal readback."},
                scoring_config={
                    "prompt": "Visualize the authority chain.",
                    "options": [],
                    "source_refs": [source_locator],
                    "hints": ["Connect execution to readback."],
                    "representation": "penecho_canvas",
                    "remediation_tags": [],
                    "model_version": "openfang-canvas-v1",
                    "prompt_version": "canvas-rubric-v1",
                    "source_version": f"course-r{course_revision}",
                    "presentation": {"canvas_href": "http://127.0.0.1:3888"},
                },
                rubric=[{
                    "criterion_id": "authority",
                    "description": "Shows authority and terminal readback",
                    "points": 1,
                }],
            ),
            AdaptiveItem(
                id=final_id,
                course_id=str(COURSE_ID),
                course_revision=course_revision,
                activity_id="final-quiz",
                item_type="single_choice",
                difficulty=0.9,
                quality_status="approved",
                expected_answer={"choice_id": "readback"},
                scoring_config={
                    "prompt": "What proves terminal completion?",
                    "options": [{"id": "readback", "label": "Application readback"}],
                    "source_refs": [source_locator],
                    "hints": ["Use owner evidence."],
                    "representation": "quiz",
                    "remediation_tags": [],
                },
                rubric=[],
            ),
        )
        db.add_all(items)
        db.add_all([
            ItemConcept(
                item_id=item.id,
                concept_id=concept_id,
                course_id=str(COURSE_ID),
                course_revision=course_revision,
            )
            for item in items
        ])
    return quiz_id, canvas_id


def _event(
    event_type: LearningEventType,
    *,
    course_id: UUID | None = COURSE_ID,
    session_id: UUID | None = None,
    expected_revision: int | None = None,
    payload: dict[str, Any] | None = None,
    confirmation: ConfirmationV1 | None = None,
) -> EventEnvelopeV1:
    key = None
    if event_type in {
        LearningEventType.MATERIAL_IMPORT,
        LearningEventType.COURSE_GENERATE,
        LearningEventType.COURSE_REVIEW,
        LearningEventType.COURSE_PUBLISH,
        LearningEventType.SESSION_START,
        LearningEventType.TASK_ANSWER,
        LearningEventType.CANVAS_SAVE,
        LearningEventType.CANVAS_SUBMIT,
        LearningEventType.CANVAS_REVIEW,
    }:
        key = f"golden-{event_type.value.replace('.', '-')}-{uuid4()}"
    return EventEnvelopeV1(
        event_type=event_type,
        invocation_id=uuid4(),
        correlation_id=CORRELATION_ID,
        actor=ACTOR,
        course_id=course_id,
        session_id=session_id,
        expected_revision=expected_revision,
        idempotency_key=key,
        confirmation=confirmation,
        payload=payload or {},
    )


def _call(dispatcher: LearningDispatcher, tool: LearningToolName, event: EventEnvelopeV1) -> dict[str, Any]:
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": str(event.invocation_id),
            "method": "tools/call",
            "params": {"name": tool.value, "arguments": event.model_dump(mode="json")},
        },
        dispatcher=dispatcher,
    )
    assert response is not None
    return json.loads(response["result"]["content"][0]["text"])


@contextmanager
def _http_adapter(runtime: CourseFactoryRuntime) -> Iterator[tuple[TestClient, str]]:
    """Serve the real Course Factory HTTP adapter over ``runtime`` in-process.

    A throwaway caller key is installed for the duration of the run, so the
    deterministic path exercises the same bearer/role checks a VibeMind
    caller passes; the previous process value is restored afterwards.
    """
    key = secrets.token_urlsafe(32)
    previous = os.environ.get(SERVICE_KEY_ENV)
    os.environ[SERVICE_KEY_ENV] = key
    runtime_api.app.dependency_overrides[get_course_factory_runtime] = lambda: runtime
    try:
        with TestClient(runtime_api.app, raise_server_exceptions=False) as client:
            yield client, key
    finally:
        runtime_api.app.dependency_overrides.pop(get_course_factory_runtime, None)
        if previous is None:
            os.environ.pop(SERVICE_KEY_ENV, None)
        else:
            os.environ[SERVICE_KEY_ENV] = previous


def _http_headers(key: str, roles: str, idempotency_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "X-VibeMind-Actor-ID": ACTOR.actor_id,
        "X-VibeMind-Space-ID": "learning",
        "X-VibeMind-Roles": roles,
        "X-VibeMind-Correlation-ID": str(CORRELATION_ID),
        "Idempotency-Key": idempotency_key,
    }


def _evidence_owner(result: dict[str, Any]) -> str | None:
    evidence = result.get("evidence")
    return evidence.get("owner") if isinstance(evidence, dict) else None


def run_deterministic(workdir: Path, *, fixture_path: Path) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    artifact_root = workdir / "artifacts"
    artifact_root.mkdir()
    import_root = workdir / "imports"
    import_root.mkdir()
    engine = create_engine(f"sqlite:///{workdir / 'golden.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    learnhouse = _LearnHouse()
    ui = _UiRecorder()

    (import_root / "authority.md").write_bytes(fixture_path.read_bytes())

    repository = CourseFactoryRepository(factory)
    factory_store = CourseFactoryArtifactStore(factory, artifact_root=artifact_root)
    publisher = CourseDraftPublisher(
        repository=repository, artifact_store=factory_store, learnhouse=learnhouse
    )
    generation_model = _GenerationGateway()
    runner = CourseFactoryRunner(
        repository=repository,
        artifact_store=factory_store,
        catalog=SourceCatalogLoader(factory),
        team=CourseAgentTeam(repository, generation_model, factory_store),
        quality_gate=QualityGate(repository),
        publisher=publisher,
    )
    adaptive = AdaptiveSessionService(
        factory, external_rubric_types=frozenset({"penecho_canvas"})
    )
    penecho = _PenEcho()
    canvas = CanvasGateway(
        penecho=penecho,
        artifacts=CanvasArtifactStore(artifact_root=artifact_root),
        evaluations=CanvasAdaptiveSubmissionService(
            session_factory=factory, gateway=_CanvasGateway()
        ),
        tutor=_Tutor(),
    )
    gateways = {
        LearningToolName.STATUS: StructuralStatusGateway(),
        LearningToolName.MATERIAL_IMPORT: MaterialImportGateway(
            learnhouse=learnhouse,
            pipeline=IngestionPipeline(
                SourceRepository(factory, artifact_root=artifact_root),
                artifact_root=artifact_root,
            ),
            session_factory=factory,
            artifact_root=artifact_root,
        ),
        **build_generation_gateways(
            application=CourseFactoryApplication(
                session_factory=factory,
                repository=repository,
                publisher=publisher,
                artifact_store=factory_store,
            ),
            learnhouse_fallback=learnhouse,
        ),
        **build_navigation_gateways(learnhouse),
        **build_session_gateways(adaptive),
        **build_canvas_gateways(canvas),
    }
    dispatcher = LearningDispatcher(
        gateways=gateways, receipts=SqlReceiptStore(factory), ui_delivery=ui
    )
    steps: list[dict[str, Any]] = []
    with _http_adapter(CourseFactoryRuntime(
        dispatcher=dispatcher,
        session_factory=factory,
        stager=MaterialStagingService(
            session_factory=factory, import_root=import_root, artifact_root=artifact_root
        ),
    )) as (http, http_key):
        report = _run_steps(
            steps=steps,
            dispatcher=dispatcher,
            http=http,
            http_key=http_key,
            factory=factory,
            learnhouse=learnhouse,
            generation_model=generation_model,
            runner=runner,
            penecho=penecho,
            ui=ui,
        )
    engine.dispose()
    return report


def _run_steps(
    *,
    steps: list[dict[str, Any]],
    dispatcher: LearningDispatcher,
    http: TestClient,
    http_key: str,
    factory: Any,
    learnhouse: _LearnHouse,
    generation_model: _GenerationGateway,
    runner: CourseFactoryRunner,
    penecho: _PenEcho,
    ui: _UiRecorder,
) -> dict[str, Any]:
    def record(name: str, transport: str, result: dict[str, Any]) -> dict[str, Any]:
        aggregate = result.get("aggregate")
        steps.append({
            "name": name,
            "transport": transport,
            "correlation_id": result.get("correlation_id"),
            "revision": (
                aggregate.get("revision") if isinstance(aggregate, dict) else result.get("revision")
            ),
            "evidence_owner": _evidence_owner(result),
            "result": result,
        })
        return result

    def invoke(name: str, tool: LearningToolName, event: EventEnvelopeV1) -> dict[str, Any]:
        return record(name, "mcp", _call(dispatcher, tool, event))

    def post(name: str, path: str, *, roles: str, body: dict[str, Any]) -> dict[str, Any]:
        response = http.post(
            path,
            headers=_http_headers(http_key, roles, f"golden-http-{name}-{uuid4()}"),
            json=body,
        )
        if response.status_code not in {200, 202}:
            raise RuntimeError(f"golden HTTP step {name} failed: {response.status_code}")
        return record(name, "http", response.json())

    invoke(
        "status",
        LearningToolName.STATUS,
        _event(LearningEventType.STATUS, course_id=None),
    )
    imported = post(
        "material_import",
        "/api/v1/materials/import",
        roles="learning.author",
        body={
            "course_id": str(COURSE_ID),
            "expected_course_revision": 1,
            "title": "Authorized AI execution",
            "source": {"kind": "local_import", "relative_path": "authority.md"},
        },
    )
    index = _Index()
    assert QdrantOutboxWorker(factory, index=index, embedder=_Embedder()).run_once()
    with factory() as db:
        source_revision = db.get(LearningSourceRevision, (str(SOURCE_ID), 1))
        chunk = db.scalar(select(LearningSourceChunk).where(
            LearningSourceChunk.source_id == str(SOURCE_ID)
        ))
    assert source_revision is not None and chunk is not None
    index_hits = index.retrieve([chunk.id])
    steps.append({
        "name": "rag_index",
        "transport": "worker",
        "evidence_owner": None,
        "result": {
            "state": "completed",
            "correlation_id": str(CORRELATION_ID),
            "point_ids": [hit.point_id for hit in index_hits],
            "source_locators": imported["locators"],
        },
    })

    generated = post(
        "course_generate",
        "/api/v1/course-factory/jobs",
        roles="learning.author",
        body={
            "course_id": str(COURSE_ID),
            "expected_course_revision": learnhouse.course_revision,
            "title": "Authorized AI execution",
            "audience": "AI course students",
            "level": "beginner",
            "locale": "en",
            "learning_objectives": ["Operate an authorized AI workflow"],
            "source_ids": [str(SOURCE_ID)],
        },
    )
    job_id = generated["job_id"]
    invoke(
        "generation_status",
        LearningToolName.GENERATION_STATUS,
        _event(LearningEventType.GENERATION_STATUS, payload={"job_id": job_id}),
    )
    generation_model.draft = _draft(job_id, source_revision, chunk)
    generation_run = asyncio.run(runner.run_job(job_id))
    assert generation_run.job.state is FactoryState.REVIEW_READY
    reviewed = invoke(
        "course_review",
        LearningToolName.COURSE_REVIEW,
        _event(
            LearningEventType.COURSE_REVIEW,
            expected_revision=generation_run.job.revision,
            payload={"job_id": job_id, "review": "approved"},
        ),
    )
    assert reviewed["state"] == "approval_required"
    published = invoke(
        "course_publish",
        LearningToolName.COURSE_PUBLISH,
        _event(
            LearningEventType.COURSE_PUBLISH,
            expected_revision=generation_run.job.revision,
            confirmation=ConfirmationV1(
                confirmed=True, approval_ref="golden-course-approval"
            ),
            payload={"job_id": job_id},
        ),
    )
    assert published["result"]["state"] == "published"
    course_revision = learnhouse.course_revision
    source_locator = f"source://{SOURCE_ID}/chunks/authority"
    quiz_id, canvas_id = _seed_adaptive(
        factory, course_revision=course_revision, source_locator=source_locator
    )

    invoke(
        "chapter_open",
        LearningToolName.CHAPTER_OPEN,
        _event(
            LearningEventType.CHAPTER_OPEN,
            payload={"chapter_id": str(CHAPTER_ID)},
        ),
    )
    started = invoke(
        "session_start",
        LearningToolName.SESSION_START,
        _event(
            LearningEventType.SESSION_START,
            payload={
                "course_revision": course_revision,
                "mode": "training",
                "blueprint": {"max_attempts": 3},
            },
        ),
    )
    turn = started["result"]["session"]
    assert turn["task"]["id"] == quiz_id
    answered = invoke(
        "task_answer",
        LearningToolName.TASK_ANSWER,
        _event(
            LearningEventType.TASK_ANSWER,
            session_id=UUID(turn["session_id"]),
            expected_revision=turn["session_revision"],
            payload={"answer": {"choice_id": "openfang"}},
        ),
    )
    canvas_turn = answered["result"]["session"]
    assert canvas_turn["task"]["id"] == canvas_id
    launch = PenEchoLaunchContextV1(
        activity_id=UUID(canvas_id),
        session_id=UUID(canvas_turn["session_id"]),
        course_id=COURSE_ID,
        course_revision=course_revision,
        actor_id=ACTOR.actor_id,
        mode="training",
        help_policy=CanvasHelpPolicyV1(
            hint_mode="selected_region", tutor_allowed=True, max_hints=2
        ),
        source_refs=(SourceReferenceV1(
            source_id=str(SOURCE_ID),
            revision=1,
            locator=source_locator,
            title="Authorized AI execution",
        ),),
        penecho_origin="http://127.0.0.1:3888",
        bridge_session_ref="golden-bridge",
    )
    penecho.register(launch)
    common = {"session_ref": "golden-bridge"}
    invoke(
        "canvas_open",
        LearningToolName.CANVAS_OPEN,
        _event(
            LearningEventType.CANVAS_OPEN,
            session_id=launch.session_id,
            payload={**common, "activity_id": canvas_id},
        ),
    )
    assert penecho.document is not None
    drawing = penecho.document.model_copy(update={"objects": (CanvasObjectV1(
        object_id="authority",
        object_type="text",
        x=20,
        y=20,
        width=420,
        height=48,
        rotation=0,
        text="OpenFang authorizes execution; readback proves completion.",
    ),)})
    invoke(
        "canvas_save",
        LearningToolName.CANVAS_SAVE,
        _event(
            LearningEventType.CANVAS_SAVE,
            session_id=launch.session_id,
            expected_revision=0,
            payload={
                **common,
                "document": drawing.model_dump(mode="json"),
                "snapshot_png_base64": base64.b64encode(PNG).decode(),
            },
        ),
    )
    submitted = invoke(
        "canvas_submit",
        LearningToolName.CANVAS_SUBMIT,
        _event(
            LearningEventType.CANVAS_SUBMIT,
            session_id=launch.session_id,
            expected_revision=1,
            confirmation=ConfirmationV1(
                confirmed=True, approval_ref="golden-canvas-approval"
            ),
            payload={
                **common,
                "submission_id": str(uuid4()),
                "adaptive_session_revision": canvas_turn["session_revision"],
            },
        ),
    )
    evaluation_id = submitted["result"]["evaluation_id"]
    invoke(
        "canvas_review",
        LearningToolName.CANVAS_REVIEW,
        _event(
            LearningEventType.CANVAS_REVIEW,
            session_id=launch.session_id,
            payload={**common, "evaluation_id": evaluation_id},
        ),
    )
    next_turn = submitted["result"]["session"]
    invoke(
        "task_next",
        LearningToolName.TASK_NEXT,
        _event(
            LearningEventType.TASK_NEXT,
            session_id=UUID(next_turn["session_id"]),
        ),
    )
    progress = invoke(
        "progress_show",
        LearningToolName.PROGRESS_SHOW,
        _event(
            LearningEventType.PROGRESS_SHOW,
            session_id=UUID(next_turn["session_id"]),
        ),
    )
    with factory() as db:
        receipt_count = db.scalar(select(func.count()).select_from(LearningInvocationReceipt))
        terminal_count = db.scalar(select(func.count()).select_from(LearningInvocationReceipt).where(
            LearningInvocationReceipt.terminal.is_(True)
        ))
        mastery_count = db.scalar(select(func.count()).select_from(MasteryEvidence))
        evaluation_count = db.scalar(select(func.count()).select_from(LearningEvaluation))
        mastery = db.scalar(select(ConceptMastery).where(
            ConceptMastery.actor_id == ACTOR.actor_id
        ))
    report = {
        "profile": "deterministic",
        "live_provider_claim": False,
        "correlation_id": str(CORRELATION_ID),
        "steps": steps,
        "factory": {
            "job_id": job_id,
            "state": published["result"]["state"],
            "roles": generation_model.roles,
            "provider_evidence": "openfang://deterministic/*",
        },
        "rag": {
            "point_count": len(index.points),
            "source_locators": imported["locators"],
        },
        "adaptive": {
            "mastery_evidence_count": mastery_count,
            "evaluation_count": evaluation_count,
            "mastery": mastery.alpha / (mastery.alpha + mastery.beta) if mastery else None,
            "next_task_id": next_turn["task"]["id"],
            "progress": progress["result"]["progress"],
        },
        "persistence": {
            "receipt_count": receipt_count,
            "terminal_receipt_count": terminal_count,
        },
        "ui_intents": ui.intents,
    }
    return report
