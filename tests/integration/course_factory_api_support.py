"""Shared harness for the Course Factory HTTP adapter tests.

The harness wires the real ``CourseFactoryApplication`` and material gateway
onto SQLite and a recording LearnHouse stand-in, then exposes the FastAPI app
with the runtime dependency overridden. Nothing here talks to a network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.bridge.dispatcher import ApplicationOutcomeV1, LearningDispatcher
from spaces.learning.contracts.events import LearningToolName
from spaces.learning.contracts.mcp_models import ToolRequestV1
from spaces.learning.contracts.outcomes import (
    AggregateRefV1,
    EvidenceRefV1,
    TruthReadbackV1,
)
from spaces.learning.deployment import runtime_api
from spaces.learning.deployment.course_factory_routes import (
    CourseFactoryRuntime,
    get_course_factory_runtime,
)
from spaces.learning.deployment.vibemind_auth import SERVICE_KEY_ENV
from spaces.learning.mcp.tools.generation import build_generation_gateways
from spaces.learning.mcp.tools.materials import MaterialImportGateway
from spaces.learning.services.course_factory.application import CourseFactoryApplication
from spaces.learning.services.course_factory.artifact_store import (
    CourseFactoryArtifactStore,
)
from spaces.learning.services.course_factory.repository import CourseFactoryRepository
from spaces.learning.services.db.models import Base, LearningArtifact
from spaces.learning.services.db.repository import SqlReceiptStore
from spaces.learning.services.ingestion.models import (
    LearningSource,
    LearningSourceRevision,
)
from spaces.learning.services.ingestion.pipeline import IngestionPipeline
from spaces.learning.services.ingestion.repository import SourceRepository
from spaces.learning.services.ingestion.material_staging import MaterialStagingService


SERVICE_KEY = "k" * 32
ALL_ROLES = "learning.author,learning.reviewer,learning.publisher"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Publisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, **kwargs: Any):
        self.calls += 1
        raise AssertionError("tests reject publication before the publisher")


@dataclass
class RecordingLearnHouse:
    """Owns course reads and source creation the way the LearnHouse client does."""

    courses: dict[str, int] = field(default_factory=dict)
    calls: list[LearningToolName] = field(default_factory=list)

    def execute(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        self.calls.append(request.tool)
        if request.tool is LearningToolName.COURSE_OPEN:
            course_id = str(request.event.course_id)
            revision = self.courses.get(course_id)
            if revision is None:
                return ApplicationOutcomeV1(
                    state="rejected",
                    error={"code": "not_found", "message": "course not found"},
                )
            return ApplicationOutcomeV1(
                state="completed",
                aggregate=AggregateRefV1(
                    aggregate_type="course", aggregate_id=course_id, revision=revision
                ),
                result={"course_id": course_id, "title": "Grounded", "published": False},
            )
        if request.tool is LearningToolName.MATERIAL_IMPORT:
            return ApplicationOutcomeV1(
                state="completed",
                aggregate=AggregateRefV1(
                    aggregate_type="source", aggregate_id=str(uuid4()), revision=1
                ),
                result={"imported": True},
            )
        raise AssertionError(f"unexpected LearnHouse tool {request.tool}")

    def readback(
        self, request: ToolRequestV1, outcome: ApplicationOutcomeV1
    ) -> TruthReadbackV1 | None:
        assert outcome.aggregate is not None
        return TruthReadbackV1(
            invocation_id=request.event.invocation_id,
            correlation_id=request.event.correlation_id,
            owner="learnhouse",
            terminal_state="completed",
            aggregate=outcome.aggregate,
            evidence=EvidenceRefV1(
                owner="learnhouse",
                evidence_id=f"learnhouse:{outcome.aggregate.aggregate_id}",
                evidence_type="application_readback",
            ),
        )


class ApiHarness:
    def __init__(self, tmp_path: Path, *, stager: MaterialStagingService | None = None):
        engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        self.artifact_root = tmp_path / "artifacts"
        self.artifact_root.mkdir()
        self.import_root = tmp_path / "imports"
        self.import_root.mkdir()
        self.course_id = str(uuid4())
        self.learnhouse = RecordingLearnHouse(courses={self.course_id: 4})
        self.repository = CourseFactoryRepository(self.session_factory)
        self.publisher = Publisher()
        self.application = CourseFactoryApplication(
            session_factory=self.session_factory,
            repository=self.repository,
            publisher=self.publisher,
            artifact_store=CourseFactoryArtifactStore(
                self.session_factory, artifact_root=self.artifact_root
            ),
        )
        gateways = {
            **build_generation_gateways(
                application=self.application, learnhouse_fallback=self.learnhouse
            ),
            LearningToolName.COURSE_OPEN: self.learnhouse,
            LearningToolName.MATERIAL_IMPORT: MaterialImportGateway(
                learnhouse=self.learnhouse,
                pipeline=IngestionPipeline(
                    SourceRepository(self.session_factory, artifact_root=self.artifact_root),
                    artifact_root=self.artifact_root,
                ),
                session_factory=self.session_factory,
                artifact_root=self.artifact_root,
            ),
        }
        self.receipts = SqlReceiptStore(self.session_factory)
        self.dispatcher = LearningDispatcher(gateways=gateways, receipts=self.receipts)
        self.stager = stager or MaterialStagingService(
            session_factory=self.session_factory,
            import_root=self.import_root,
            artifact_root=self.artifact_root,
        )
        self.ready_source = self.add_source(status="stored")

    def add_source(self, *, status: str) -> str:
        source_id = str(uuid4())
        artifact_id = str(uuid4())
        content_hash = sha256(source_id.encode())
        with self.session_factory() as session, session.begin():
            session.add(LearningArtifact(
                id=artifact_id,
                relative_path=f"sources/{source_id}.txt",
                content_hash=content_hash,
                media_type="text/plain",
                size_bytes=36,
                aggregate_type="source",
                aggregate_id=source_id,
                revision=1,
            ))
            session.add(LearningSource(
                id=source_id, course_id=self.course_id, title="Authority", current_revision=1
            ))
            session.add(LearningSourceRevision(
                source_id=source_id,
                revision=1,
                artifact_id=artifact_id,
                content_hash=content_hash,
                ingestion_spec_version="ingestion-v1",
                media_type="text/plain",
                size_bytes=36,
                status=status,
            ))
        return source_id

    def runtime(self) -> CourseFactoryRuntime:
        return CourseFactoryRuntime(
            dispatcher=self.dispatcher,
            session_factory=self.session_factory,
            stager=self.stager,
        )

    def client(self, monkeypatch) -> TestClient:
        monkeypatch.setenv(SERVICE_KEY_ENV, SERVICE_KEY)
        runtime = self.runtime()
        runtime_api.app.dependency_overrides[get_course_factory_runtime] = lambda: runtime
        return TestClient(runtime_api.app, raise_server_exceptions=False)

    def generation_payload(self, *source_ids: str, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "course_id": self.course_id,
            "expected_course_revision": 4,
            "title": "Industrial AI Safety",
            "audience": "Maintenance engineers",
            "level": "advanced",
            "locale": "de-DE",
            "learning_objectives": ["Assess AI-assisted maintenance risks"],
            "source_ids": list(source_ids or (self.ready_source,)),
        }
        payload.update(overrides)
        return payload


def headers(
    roles: str = ALL_ROLES,
    idempotency_key: str | None = None,
    *,
    key: str = SERVICE_KEY,
    space: str = "learning",
) -> dict[str, str]:
    value = {
        "Authorization": f"Bearer {key}",
        "X-VibeMind-Actor-ID": "local-owner",
        "X-VibeMind-Space-ID": space,
        "X-VibeMind-Roles": roles,
        "X-VibeMind-Correlation-ID": str(uuid4()),
    }
    if idempotency_key is not None:
        value["Idempotency-Key"] = idempotency_key
    return value


def clear_overrides() -> None:
    runtime_api.app.dependency_overrides.clear()
