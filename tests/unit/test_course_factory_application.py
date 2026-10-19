from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.contracts.events import LearningEventType, LearningToolName
from spaces.learning.contracts.mcp_models import (
    ActorV1,
    ConfirmationV1,
    EventEnvelopeV1,
    ToolRequestV1,
)
from spaces.learning.mcp.tools.generation import CourseFactoryGateway
from spaces.learning.services.course_factory.application import CourseFactoryApplication
from spaces.learning.services.course_factory.artifact_store import (
    CourseFactoryArtifactStore,
)
from spaces.learning.services.course_factory.repository import CourseFactoryRepository
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.db.models import Base, LearningArtifact
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.services.ingestion.models import (
    LearningSource,
    LearningSourceRevision,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class _Publisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, **kwargs):
        self.calls += 1
        raise AssertionError("publication must be rejected before the publisher")


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        engine = create_engine(f"sqlite:///{tmp_path / 'application.db'}")
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        root = tmp_path / "artifacts"
        root.mkdir()
        self.course_id = str(uuid4())
        self.ready_source = self._source(status="stored")
        self.failed_source = self._source(status="failed")
        self.repository = CourseFactoryRepository(self.session_factory)
        self.publisher = _Publisher()
        self.application = CourseFactoryApplication(
            session_factory=self.session_factory,
            repository=self.repository,
            publisher=self.publisher,
            artifact_store=CourseFactoryArtifactStore(
                self.session_factory, artifact_root=root
            ),
        )

    def _source(self, *, status: str) -> str:
        source_id = str(uuid4())
        artifact_id = str(uuid4())
        with self.session_factory() as session, session.begin():
            session.add(LearningArtifact(
                id=artifact_id,
                relative_path=f"sources/{source_id}.txt",
                content_hash=_hash(source_id),
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
                content_hash=_hash(source_id),
                ingestion_spec_version="ingestion-v1",
                media_type="text/plain",
                size_bytes=36,
                status=status,
            ))
        return source_id


@pytest.fixture()
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


def _request(
    tool: LearningToolName,
    event_type: LearningEventType,
    *,
    course_id: str | None,
    payload: dict[str, object],
    expected_revision: int | None = None,
    confirmation: ConfirmationV1 | None = None,
) -> ToolRequestV1:
    return ToolRequestV1(
        tool=tool,
        event=EventEnvelopeV1(
            event_type=event_type,
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            actor=ActorV1(actor_id="local-owner", actor_type="local_user"),
            course_id=course_id,
            idempotency_key=f"key-{uuid4()}",
            expected_revision=expected_revision,
            confirmation=confirmation,
            payload=payload,
        ),
    )


def _generate(env: _Env, *source_ids: str) -> ToolRequestV1:
    return _request(
        LearningToolName.COURSE_GENERATE,
        LearningEventType.COURSE_GENERATE,
        course_id=env.course_id,
        expected_revision=2,
        payload={
            "audience": "Engineers",
            "target_outcome": "Assess AI risk",
            "source_ids": list(source_ids),
        },
    )


def test_application_creates_source_grounded_queued_job(env: _Env) -> None:
    result = env.application.execute(_generate(env, env.ready_source))

    assert result.state == "completed"
    assert result.result["state"] == "queued"
    assert result.aggregate.aggregate_type == "course_factory_job"


def test_application_rejects_unindexed_or_unknown_source(env: _Env) -> None:
    for source in (env.failed_source, str(uuid4())):
        result = env.application.execute(_generate(env, source))
        assert result.state == "rejected"
        assert result.error.code == "source_not_ready"


def test_application_reports_unknown_job_as_not_found(env: _Env) -> None:
    result = env.application.execute(_request(
        LearningToolName.GENERATION_STATUS,
        LearningEventType.GENERATION_STATUS,
        course_id=None,
        payload={"job_id": str(uuid4())},
    ))
    assert result.state == "rejected"
    assert result.error.code == "not_found"


def test_application_status_readback_matches_the_durable_job(env: _Env) -> None:
    created = env.application.execute(_generate(env, env.ready_source))
    request = _request(
        LearningToolName.GENERATION_STATUS,
        LearningEventType.GENERATION_STATUS,
        course_id=env.course_id,
        payload={"job_id": created.result["job_id"]},
    )
    status = env.application.execute(request)
    readback = env.application.readback(request, status)

    assert status.aggregate == created.aggregate
    assert readback is not None
    assert readback.evidence.evidence_type == "job_readback"


def _queued_job(env: _Env) -> tuple[str, int]:
    created = env.application.execute(_generate(env, env.ready_source))
    return created.result["job_id"], created.result["revision"]


def test_review_rejects_stale_revision(env: _Env) -> None:
    job_id, revision = _queued_job(env)
    result = env.application.execute(_request(
        LearningToolName.COURSE_REVIEW,
        LearningEventType.COURSE_REVIEW,
        course_id=env.course_id,
        expected_revision=revision + 5,
        payload={"job_id": job_id, "review": "rejected"},
    ))
    assert result.state == "rejected"
    assert result.error.code == "revision_conflict"


def test_review_approval_requires_explicit_publication(env: _Env) -> None:
    job_id, revision = _queued_job(env)
    result = env.application.execute(_request(
        LearningToolName.COURSE_REVIEW,
        LearningEventType.COURSE_REVIEW,
        course_id=env.course_id,
        expected_revision=revision,
        payload={"job_id": job_id, "review": "approved"},
    ))
    assert result.state == "approval_required"
    assert result.result["publish_confirmation_required"] is True


def test_publish_rejects_stale_revision_and_unreviewed_jobs(env: _Env) -> None:
    job_id, revision = _queued_job(env)
    confirmation = ConfirmationV1(confirmed=True, approval_ref="approval-1")
    stale = env.application.execute(_request(
        LearningToolName.COURSE_PUBLISH,
        LearningEventType.COURSE_PUBLISH,
        course_id=env.course_id,
        expected_revision=revision + 1,
        confirmation=confirmation,
        payload={"job_id": job_id},
    ))
    unreviewed = env.application.execute(_request(
        LearningToolName.COURSE_PUBLISH,
        LearningEventType.COURSE_PUBLISH,
        course_id=env.course_id,
        expected_revision=revision,
        confirmation=confirmation,
        payload={"job_id": job_id},
    ))

    assert stale.error.code == "revision_conflict"
    assert unreviewed.error.code == "not_review_ready"
    assert env.repository.get(job_id).state is FactoryState.QUEUED
    assert env.publisher.calls == 0


def test_persistence_conflict_maps_to_revision_conflict(env: _Env, monkeypatch) -> None:
    def conflict(**kwargs):
        raise PersistenceConflict("course factory job conflict")

    monkeypatch.setattr(env.repository, "create_job", conflict)
    result = env.application.execute(_generate(env, env.ready_source))
    assert result.state == "rejected"
    assert result.error.code == "revision_conflict"


def test_mcp_gateway_is_a_thin_adapter_over_the_application(env: _Env) -> None:
    gateway = CourseFactoryGateway(env.application)
    request = _generate(env, env.ready_source)
    outcome = gateway.execute(request)

    assert outcome.state == "completed"
    assert gateway.readback(request, outcome) is not None
    assert gateway.application is env.application
