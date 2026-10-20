from __future__ import annotations

from spaces.learning.services.course_factory.chapter_verification import fragment_from_draft

import json

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.course_factory.artifact_store import (
    CourseFactoryArtifactStore,
)
from spaces.learning.services.course_factory.catalog import SourceCatalogLoader
from spaces.learning.services.course_factory.model_gateway import GatewayResult
from spaces.learning.services.course_factory.quality_gate import QualityGate
from spaces.learning.services.course_factory.publisher import (
    CourseDraftPublisher,
    DraftDeliveryReceipt,
)
from spaces.learning.services.course_factory.repository import (
    CourseFactoryRepository,
    GenerationRequest,
    SourceProvenance,
    StageArtifactInput,
)
from spaces.learning.services.course_factory.roles.schemas import (
    ArchitectOutput,
    AssessmentOutput,
    ConceptMapOutput,
    LessonOutput,
    QualityReviewOutput,
)
from spaces.learning.services.course_factory.runner import CourseFactoryRunner
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.course_factory.team import CourseAgentTeam
from spaces.learning.mcp.tools.generation import CourseFactoryGateway
from spaces.learning.contracts.events import LearningEventType, LearningToolName
from spaces.learning.contracts.mcp_models import (
    ActorV1,
    EventEnvelopeV1,
    ToolRequestV1,
)
from spaces.learning.services.db.models import Base, LearningArtifact
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.services.ingestion.models import (
    LearningSource,
    LearningSourceChunk,
    LearningSourceRevision,
)
from spaces.learning.tests.unit.test_source_verification import _draft


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture()
def generation_context(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'generation.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    ids = {
        name: str(uuid4())
        for name in ("course", "source", "chunk", "citation", "job")
    }
    with factory() as session, session.begin():
        session.add(
            LearningArtifact(
                id=str(uuid4()),
                relative_path="sources/source.txt",
                content_hash="a" * 64,
                media_type="text/plain",
                size_bytes=8,
                aggregate_type="source",
                aggregate_id=ids["source"],
                revision=1,
            )
        )
        artifact_id = session.query(LearningArtifact.id).scalar()
        session.add(
            LearningSource(
                id=ids["source"],
                course_id=ids["course"],
                title="Authority",
                current_revision=2,
            )
        )
        session.add(
            LearningSourceRevision(
                source_id=ids["source"],
                revision=2,
                artifact_id=artifact_id,
                content_hash="a" * 64,
                ingestion_spec_version="ingestion-v1",
                media_type="text/plain",
                size_bytes=8,
                status="stored",
            )
        )
        session.add(
            LearningSourceChunk(
                id=ids["chunk"],
                source_id=ids["source"],
                source_revision=2,
                ordinal=0,
                content="Provider execution stays behind OpenFang.",
                content_hash="b" * 64,
                locator={"page": 4, "heading": "Authority"},
                locator_hash=_hash("locator"),
                metadata_json={},
            )
        )
    repository = CourseFactoryRepository(factory)
    job = repository.create_job(
        course_id=ids["course"],
        request_hash=_hash("generation-request"),
        provenance=(
            SourceProvenance(
                source_id=ids["source"], revision=2, content_hash="a" * 64
            ),
        ),
        generation_request=GenerationRequest(
            correlation_id=str(uuid4()),
            audience="AI operations students",
            target_outcome="Course: Grounded AI Operations\nOperate an authorized AI workflow",
        ),
    )
    ids["job"] = job.id
    draft = _draft(ids)
    draft.job_id = job.id
    return factory, artifact_root, repository, job, draft


class _GenerationGateway:
    def __init__(
        self, draft, *, unsupported: tuple[str, ...] = (), review_decision: str = "pass"
    ) -> None:
        self.draft = draft
        self.unsupported = unsupported
        self.review_decision = review_decision
        self.calls: list[str] = []
        self.payloads: list[object] = []

    async def generate(self, invocation, output_model):
        self.calls.append(invocation.role)
        self.payloads.append(invocation.input_payload)
        values = {
            "architect": ArchitectOutput(
                schema_version="architect-v1",
                audience="Students",
                prerequisites=[],
                outcomes=["Operate an authorized workflow"],
                chapters=["Foundations"],
            ),
            "concept_mapper": ConceptMapOutput(
                schema_version="concept-map-v1",
                concepts=["authority"],
                dependencies=[],
                chapter_coverage={"Foundations": ["authority"]},
            ),
            "lesson_author": LessonOutput(
                schema_version="lesson-v1",
                lessons=[
                    {
                        "chapter": "Foundations",
                        "title": "Authority",
                        "body": "Provider execution stays behind OpenFang.",
                    }
                ],
            ),
            "assessment_designer": AssessmentOutput(
                schema_version="assessment-v1",
                activities=[
                    {
                        "concept": "authority",
                        "type": "case",
                        "prompt": "Choose the authorized path.",
                    }
                ],
            ),
            "chapter_verifier": fragment_from_draft(
                self.draft,
                int(invocation.input_payload.get("chapter_index", 1)),
                self.unsupported,
            )
            if invocation.role == "chapter_verifier"
            else None,
            "quality_reviewer": QualityReviewOutput(
                schema_version="quality-review-v1",
                decision=self.review_decision,
                issues=[] if self.review_decision == "pass" else ["Make the tasks apply the rules."],
                score=0.9,
            ),
        }
        output = values[invocation.role]
        assert isinstance(output, output_model)
        return GatewayResult(
            output=output,
            evidence_ref=f"openfang://completion/{invocation.role}",
        )


class _LearnHouse:
    def __init__(self) -> None:
        self.deliveries: list[str] = []

    async def stage_draft(self, **kwargs):
        self.deliveries.append(kwargs["draft_hash"])
        return DraftDeliveryReceipt(
            course_id=kwargs["course_id"],
            factory_job_id=kwargs["draft"].job_id,
            attempt_number=kwargs["draft"].attempt_number,
            draft_hash=kwargs["draft_hash"],
            learnhouse_revision=kwargs["expected_revision"] + 1,
            evidence_ref="learnhouse://factory-draft/readback",
        )

    async def approve(self, **kwargs):
        raise AssertionError("generation must not approve a draft")

    async def publish(self, **kwargs):
        raise AssertionError("generation must not publish a draft")

    async def read_course(self, **kwargs):
        raise AssertionError("generation must not read a published course")


def _runner(factory, root, repository, gateway, learnhouse=None, max_attempts=3):
    store = CourseFactoryArtifactStore(factory, artifact_root=root)
    learnhouse = learnhouse or _LearnHouse()
    return CourseFactoryRunner(
        repository=repository,
        artifact_store=store,
        catalog=SourceCatalogLoader(factory),
        team=CourseAgentTeam(repository, gateway, store),
        quality_gate=QualityGate(repository),
        publisher=CourseDraftPublisher(
            repository=repository,
            artifact_store=store,
            learnhouse=learnhouse,
        ),
        max_attempts=max_attempts,
    )


@pytest.mark.asyncio
async def test_grounded_generation_reaches_review_ready_and_survives_readback(
    generation_context,
) -> None:
    factory, root, repository, job, draft = generation_context
    gateway = _GenerationGateway(draft)

    result = await _runner(factory, root, repository, gateway).run_job(job.id)

    assert result.job.state is FactoryState.REVIEW_READY
    assert result.report.approved is True
    assert gateway.calls[-1] == "quality_reviewer"
    assert repository.get(job.id).state is FactoryState.REVIEW_READY
    assert repository.get_draft_delivery(job.id).learnhouse_revision == 2
    restarted_store = CourseFactoryArtifactStore(factory, artifact_root=root)
    verifying = restarted_store.read_stage_output(
        job.id, attempt_number=1, stage=FactoryState.VERIFYING
    )
    assert verifying["source_verifier"]["draft"]["job_id"] == job.id


@pytest.mark.asyncio
async def test_grounded_generation_accepts_indexed_source_revision(
    generation_context,
) -> None:
    factory, root, repository, job, draft = generation_context
    with factory() as session, session.begin():
        revision = session.get(LearningSourceRevision, (draft.source_provenance[0].source_id, 2))
        assert revision is not None
        revision.status = "indexed"
    gateway = _GenerationGateway(draft)

    result = await _runner(factory, root, repository, gateway).run_job(job.id)

    assert result.job.state is FactoryState.REVIEW_READY
    assert result.report.approved is True


@pytest.mark.asyncio
async def test_unsupported_claim_stays_visible_without_ai_quality_call(
    generation_context,
) -> None:
    factory, root, repository, job, draft = generation_context
    gateway = _GenerationGateway(draft, unsupported=("claim-authority",))

    result = await _runner(factory, root, repository, gateway).run_job(job.id)

    assert result.job.state is FactoryState.QUALITY_GATE
    assert result.report.approved is False
    assert result.report.issue_codes == ["unsupported_claim"]
    assert "quality_reviewer" not in gateway.calls
    status_gateway = CourseFactoryGateway(
        session_factory=factory,
        repository=repository,
        publisher=object(),
        artifact_store=CourseFactoryArtifactStore(factory, artifact_root=root),
    )
    status = status_gateway.execute(
        ToolRequestV1(
            tool=LearningToolName.GENERATION_STATUS,
            event=EventEnvelopeV1(
                event_type=LearningEventType.GENERATION_STATUS,
                invocation_id=uuid4(),
                correlation_id=uuid4(),
                actor=ActorV1(
                    actor_id="local-owner", actor_type="local_user"
                ),
                course_id=result.job.course_id,
                payload={"job_id": result.job.id},
            ),
        )
    )
    assert status.result is not None
    # Chapter-wise verification prefixes ids with the chapter (k1-, k2-, ...).
    assert status.result["unsupported_claim_ids"] == ["k1-claim-authority"]
    # Why the draft is held: live only unsupported claims were visible, other
    # reasons (e.g. draft_job_mismatch) left the job silently in quality_gate.
    assert status.result["quality_issue_codes"] == ["unsupported_claim"]
    # The person must see what they approve: the verified draft's outline.
    assert status.result["draft_summary"] == {
        "title": "Grounded AI Operations",
        "chapters": [
            {
                "title": "Foundations",
                "lessons": ["Authority boundaries"],
                "activity_count": 1,
            }
        ],
    }


@pytest.mark.asyncio
async def test_restart_resumes_from_persisted_authoring_stage(generation_context) -> None:
    factory, root, repository, job, draft = generation_context
    store = CourseFactoryArtifactStore(factory, artifact_root=root)
    job = repository.advance(
        job.id, expected_revision=job.revision, target=FactoryState.INGESTING
    )
    for stage, payload, target in (
        (
            FactoryState.INGESTING,
            {"schema_version": "factory-ingestion-v1", "sources": []},
            FactoryState.STRUCTURING,
        ),
        (
            FactoryState.STRUCTURING,
            {
                "architect": ArchitectOutput(
                    schema_version="architect-v1",
                    audience="Students",
                    prerequisites=[],
                    outcomes=["Operate an authorized workflow"],
                    chapters=["Foundations"],
                ).model_dump(mode="json"),
                "concept_mapper": ConceptMapOutput(
                    schema_version="concept-map-v1",
                    concepts=["authority"],
                    dependencies=[],
                    chapter_coverage={"Foundations": ["authority"]},
                ).model_dump(mode="json"),
            },
            FactoryState.AUTHORING,
        ),
    ):
        artifact = store.write_stage_output(job, stage=stage, payload=payload)
        repository.record_stage_artifact(
            job.id,
            expected_revision=job.revision,
            artifact=StageArtifactInput(
                stage=stage,
                input_hash=_hash(f"{stage.value}-input"),
                output_hash=artifact.content_hash,
                evidence_refs=(f"learning-artifact://{artifact.artifact_id}",),
                output_artifact=artifact,
            ),
        )
        job = repository.advance(
            job.id,
            expected_revision=job.revision,
            target=target,
        )
    gateway = _GenerationGateway(draft)

    result = await _runner(factory, root, repository, gateway).run_job(job.id)

    assert result.job.state is FactoryState.REVIEW_READY
    assert gateway.calls == [
        "lesson_author",
        "assessment_designer",
        "chapter_verifier",
        "quality_reviewer",
    ]


@pytest.mark.asyncio
async def test_unexpected_runtime_error_fails_the_job_visibly(generation_context) -> None:
    # A job must never stay in a working state after its run raised: the worker
    # only claims queued jobs, so a stuck job would hang without any signal.
    factory, root, repository, job, draft = generation_context
    gateway = _GenerationGateway(draft)

    class _BrokenRepository:
        def __init__(self, inner) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def record_stage_artifact(self, *args, **kwargs):
            raise PersistenceConflict("course factory artifact conflict")

    runner = _runner(factory, root, _BrokenRepository(repository), gateway)

    with pytest.raises(PersistenceConflict):
        await runner.run_job(job.id)

    failed = repository.get(job.id)
    assert failed.state is FactoryState.FAILED


@pytest.mark.asyncio
async def test_draft_delivery_failure_fails_the_job_visibly(generation_context) -> None:
    # Live: LearnHouse refused the draft (published course, 409) after an approved
    # gate, and the job stayed in quality_gate without any reason.
    factory, root, repository, job, draft = generation_context
    gateway = _GenerationGateway(draft)

    class _Refusing(_LearnHouse):
        async def stage_draft(self, **kwargs):
            raise RuntimeError("LearnHouse request was rejected with 409")

    with pytest.raises(RuntimeError):
        await _runner(factory, root, repository, gateway, _Refusing()).run_job(job.id)

    failed = repository.get(job.id)
    assert failed.state is FactoryState.FAILED


@pytest.mark.asyncio
async def test_status_shows_when_the_ai_review_asks_for_a_revision(generation_context) -> None:
    # Live: the AI reviewer said "revise" with concrete issues; the status showed
    # quality_gate with no reason at all.
    factory, root, repository, job, draft = generation_context
    gateway = _GenerationGateway(draft, review_decision="revise")
    # Last allowed attempt: the draft stays held and shows why.
    await _runner(factory, root, repository, gateway, max_attempts=1).run_job(job.id)

    status_gateway = CourseFactoryGateway(
        session_factory=factory,
        repository=repository,
        publisher=object(),
        artifact_store=CourseFactoryArtifactStore(factory, artifact_root=root),
    )
    status = status_gateway.execute(
        ToolRequestV1(
            tool=LearningToolName.GENERATION_STATUS,
            event=EventEnvelopeV1(
                event_type=LearningEventType.GENERATION_STATUS,
                invocation_id=uuid4(),
                correlation_id=uuid4(),
                actor=ActorV1(actor_id="local-owner", actor_type="local_user"),
                course_id=job.course_id,
                payload={"job_id": job.id},
            ),
        )
    )
    assert status.result["state"] == "quality_gate"
    assert "ai_review_not_approved" in status.result["quality_issue_codes"]
    assert status.result["ai_review_issues"] == ["Make the tasks apply the rules."]


@pytest.mark.asyncio
async def test_ai_revision_request_starts_a_new_attempt_with_the_feedback(
    generation_context,
) -> None:
    # Live: the AI reviewer asked for concrete revisions and the job just sat in
    # quality_gate. Now the feedback drives a new attempt (up to three).
    factory, root, repository, job, draft = generation_context
    gateway = _GenerationGateway(draft, review_decision="revise")

    await _runner(factory, root, repository, gateway).run_job(job.id)

    retried = repository.get(job.id)
    assert retried.state is FactoryState.QUEUED and retried.attempt_number == 2
    request = repository.get_current_attempt(job.id).generation_request
    assert request is not None
    assert request.revision_notes == ("Make the tasks apply the rules.",)
    # Attempt 1 already staged a draft in LearnHouse (revision 1 -> 2); the next
    # attempt must expect that revision (live: 409 revision_conflict).
    assert request.learnhouse_revision == 2


@pytest.mark.asyncio
async def test_feedback_reaches_the_roles_of_the_next_attempt(generation_context) -> None:
    factory, root, repository, job, draft = generation_context
    reviewing = _GenerationGateway(draft, review_decision="revise")
    await _runner(factory, root, repository, reviewing).run_job(job.id)

    retry_draft = draft.model_copy(update={"attempt_number": 2})
    seen = _GenerationGateway(retry_draft)
    claimed = repository.claim_next_queued()
    assert claimed is not None
    await _runner(factory, root, repository, seen).run_job(claimed.id)

    assert any("Make the tasks apply the rules." in json.dumps(p) for p in seen.payloads)
