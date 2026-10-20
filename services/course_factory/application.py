"""Transport-neutral Course Factory commands.

HTTP routes and the MCP gateway both submit ``ToolRequestV1`` commands through
the durable Learning dispatcher into this one application. Lifecycle
decisions live here (and below, in the repository, runner, quality gate, and
publisher), never in a transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from spaces.learning.bridge.dispatcher import ApplicationOutcomeV1
from spaces.learning.contracts.events import LearningToolName
from spaces.learning.contracts.mcp_models import ToolRequestV1
from spaces.learning.contracts.outcomes import (
    AggregateRefV1,
    EvidenceRefV1,
    ToolErrorV1,
    TruthReadbackV1,
)
from spaces.learning.services.course_factory.artifact_store import (
    CourseFactoryArtifactStore,
)
from spaces.learning.services.course_factory.catalog import SourceCatalogLoader
from spaces.learning.services.course_factory.publisher import (
    CourseDraftPublisher,
    PublishConfirmation,
)
from spaces.learning.services.course_factory.repository import (
    CourseFactoryRepository,
    FactoryJobRecord,
    GenerationRequest,
    SessionFactory,
    SourceProvenance,
    generation_request_hash,
)
from spaces.learning.services.course_factory.quality_gate import QualityGate
from spaces.learning.services.course_factory.roles.schemas import SourceVerificationOutput
from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.course_factory.state_machine import (
    FactoryState,
    InvalidFactoryTransition,
)
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.services.ingestion.models import (
    LearningSource,
    LearningSourceRevision,
)


_MAX_ATTEMPTS = 3

# (course_id, correlation_id) -> object with `published` and `revision` (LearnHouse readback).
CourseStateReader = Callable[[str, str], Awaitable[Any]]


class SourceNotReady(ValueError):
    """A requested source is unknown, stale, or not durably stored/indexed."""


class CourseFactoryApplication:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        repository: CourseFactoryRepository,
        publisher: CourseDraftPublisher,
        artifact_store: CourseFactoryArtifactStore,
        course_state: CourseStateReader | None = None,
    ) -> None:
        # Reads the LearnHouse course before a job is queued; None (tests, offline)
        # leaves the check to draft delivery as before.
        self._course_state = course_state
        self._session_factory = session_factory
        self._repository = repository
        self._publisher = publisher
        self._artifact_store = artifact_store
        self._handlers: dict[
            LearningToolName, Callable[[ToolRequestV1], ApplicationOutcomeV1]
        ] = {
            LearningToolName.COURSE_GENERATE: self._generate,
            LearningToolName.GENERATION_STATUS: self._status,
            LearningToolName.COURSE_REVIEW: self._review,
            LearningToolName.COURSE_PUBLISH: self._publish,
        }

    def execute(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        handler = self._handlers.get(request.tool)
        if handler is None:
            raise ValueError("unsupported Course Factory tool")
        try:
            return handler(request)
        except PersistenceConflict as error:
            return _rejected("revision_conflict", str(error))
        except SourceNotReady as error:
            return _rejected("source_not_ready", str(error))
        except InvalidFactoryTransition as error:
            return _rejected("invalid_transition", str(error))
        except LookupError as error:
            return _rejected("not_found", str(error))
        except ValueError as error:
            return _rejected("invalid_generation_request", str(error))

    def readback(
        self, request: ToolRequestV1, outcome: ApplicationOutcomeV1
    ) -> TruthReadbackV1 | None:
        if outcome.aggregate is None:
            return None
        try:
            job = self._repository.get(outcome.aggregate.aggregate_id)
        except LookupError:
            return None
        observed = _aggregate(job.id, job.revision)
        if observed != outcome.aggregate:
            return None
        return TruthReadbackV1(
            invocation_id=request.event.invocation_id,
            correlation_id=request.event.correlation_id,
            owner="course-factory",
            terminal_state="completed",
            aggregate=observed,
            evidence=EvidenceRefV1(
                owner="course-factory",
                evidence_id=f"factory:{job.id}:{job.revision}",
                evidence_type="job_readback",
            ),
        )

    def _generate(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        retry_job_id = _optional_uuid(request.event.payload, "retry_job_id")
        if retry_job_id is not None:
            previous = self._repository.get_current_attempt(retry_job_id)
            job = self._repository.get(retry_job_id)
            if job.attempt_number >= _MAX_ATTEMPTS:
                return _rejected(
                    "generation_attempt_limit",
                    "course generation is limited to three attempts",
                )
            if request.event.expected_revision != job.revision:
                return _rejected("revision_conflict", "factory revision is stale")
            if previous.generation_request is None:
                raise ValueError("retry has no durable generation request")
            generation_request = previous.generation_request.model_copy(
                update={"correlation_id": str(request.event.correlation_id)}
            )
            job = self._repository.retry(
                job.id,
                expected_revision=job.revision,
                request_hash=_generation_hash(
                    course_id=job.course_id,
                    request=generation_request,
                    provenance=previous.provenance,
                ),
                provenance=previous.provenance,
                generation_request=generation_request,
            )
            return _job_outcome(job)

        course_id = _required_course_id(request)
        if request.event.expected_revision is None:
            raise ValueError("course generation requires expected_revision")
        audience = _string(request.event.payload, "audience", maximum=1_000)
        target = _string(request.event.payload, "target_outcome", maximum=2_000)
        source_ids = _source_ids(request.event.payload)
        if self._course_state is not None:
            current = asyncio.run(
                self._course_state(course_id, str(request.event.correlation_id))
            )
            if current.published:
                return _rejected(
                    "course_published",
                    "the course is published and immutable; create a new course for new content",
                )
            if current.revision != request.event.expected_revision:
                return _rejected("revision_conflict", "course revision is stale")
        provenance = self._current_provenance(course_id, source_ids)
        generation_request = GenerationRequest(
            correlation_id=str(request.event.correlation_id),
            audience=audience,
            target_outcome=target,
            learnhouse_revision=request.event.expected_revision,
        )
        request_hash = _generation_hash(
            course_id=course_id,
            request=generation_request,
            provenance=provenance,
        )
        job = self._repository.create_job(
            course_id=course_id,
            request_hash=request_hash,
            provenance=provenance,
            generation_request=generation_request,
        )
        return _job_outcome(job)

    def _status(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        job = self._repository.get(_uuid(request.event.payload, "job_id"))
        if (
            request.event.course_id is not None
            and str(request.event.course_id) != job.course_id
        ):
            raise ValueError("factory job does not belong to the requested course")
        result = _job_result(job)
        if job.state in {
            FactoryState.QUALITY_GATE,
            FactoryState.REVIEW_READY,
            FactoryState.PUBLISHED,
        }:
            payload = self._artifact_store.read_stage_output(
                job.id,
                attempt_number=job.attempt_number,
                stage=FactoryState.VERIFYING,
            )
            verifier = payload.get("source_verifier")
            if isinstance(verifier, dict):
                unsupported = verifier.get("unsupported_claim_ids")
                if isinstance(unsupported, list) and all(
                    isinstance(item, str) for item in unsupported
                ):
                    result["unsupported_claim_ids"] = unsupported
                summary = _draft_summary(verifier.get("draft"))
                if summary is not None:
                    result["draft_summary"] = summary
                if job.state is FactoryState.QUALITY_GATE:
                    codes = self._quality_issue_codes(job, verifier)
                    review_issues = self._ai_review_issues(job)
                    if review_issues is not None:
                        codes = [*(codes or []), "ai_review_not_approved"]
                        result["ai_review_issues"] = review_issues
                    if codes is not None:
                        result["quality_issue_codes"] = codes
        return ApplicationOutcomeV1(
            state="completed",
            aggregate=_aggregate(job.id, job.revision),
            result=result,
        )

    def _quality_issue_codes(
        self, job: FactoryJobRecord, verifier: Mapping[str, object]
    ) -> list[str] | None:
        """Why the gate holds the draft; recomputed read-only from the verified draft."""
        try:
            verified = SourceVerificationOutput.model_validate(verifier)
            attempt = self._repository.get_current_attempt(job.id)
            catalog = SourceCatalogLoader(self._session_factory).load(
                course_id=job.course_id, provenance=attempt.provenance
            )
            report = QualityGate(self._repository).inspect(
                job.id,
                expected_revision=job.revision,
                draft=verified.draft,
                sources=catalog.snapshots,
                declared_unsupported_claim_ids=tuple(verified.unsupported_claim_ids),
            )
        except (LookupError, ValidationError, ValueError):
            return None
        return report.issue_codes

    def _ai_review_issues(self, job: FactoryJobRecord) -> list[str] | None:
        """The AI reviewer's issues when it asked for a revision, else None."""
        try:
            payload = self._artifact_store.read_stage_output(
                job.id, attempt_number=job.attempt_number, stage=FactoryState.QUALITY_GATE
            )
        except LookupError:
            return None
        review = payload.get("quality_reviewer")
        if not isinstance(review, dict) or review.get("decision") == "pass":
            return None
        issues = review.get("issues")
        if not isinstance(issues, list):
            return []
        return [str(issue)[:1_000] for issue in issues[:20]]

    def _review(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        job = self._repository.get(_uuid(request.event.payload, "job_id"))
        if _required_course_id(request) != job.course_id:
            raise ValueError("factory job does not belong to the requested course")
        if request.event.expected_revision != job.revision:
            return _rejected("revision_conflict", "factory revision is stale")
        decision = _string(request.event.payload, "review", maximum=64)
        if decision not in {"rejected", "changes_requested"}:
            return ApplicationOutcomeV1(
                state="approval_required",
                aggregate=_aggregate(job.id, job.revision),
                result={"state": job.state.value, "publish_confirmation_required": True},
            )
        job = self._repository.advance(
            job.id,
            expected_revision=job.revision,
            target=FactoryState.REJECTED,
            reason_code="review_rejected",
        )
        return _job_outcome(job)

    def _publish(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        job = self._repository.get(_uuid(request.event.payload, "job_id"))
        if _required_course_id(request) != job.course_id:
            raise ValueError("factory job does not belong to the requested course")
        confirmation = request.event.confirmation
        if confirmation is None or request.event.expected_revision is None:
            raise ValueError("factory publication requires explicit confirmation")
        if request.event.expected_revision != job.revision:
            return _rejected("revision_conflict", "factory revision is stale")
        if job.state is not FactoryState.REVIEW_READY:
            return _rejected(
                "not_review_ready", "only a review-ready factory job can be published"
            )
        published = asyncio.run(
            self._publisher.publish(
                job_id=job.id,
                expected_factory_revision=request.event.expected_revision,
                confirmation=PublishConfirmation(
                    confirmed=confirmation.confirmed,
                    approval_ref=confirmation.approval_ref,
                ),
                correlation_id=str(request.event.correlation_id),
            )
        )
        return _job_outcome(published)

    def _current_provenance(
        self, course_id: str, requested_ids: tuple[str, ...]
    ) -> tuple[SourceProvenance, ...]:
        with self._session_factory() as session:
            query = (
                select(LearningSource, LearningSourceRevision)
                .join(
                    LearningSourceRevision,
                    (LearningSourceRevision.source_id == LearningSource.id)
                    & (
                        LearningSourceRevision.revision
                        == LearningSource.current_revision
                    ),
                )
                .where(LearningSource.course_id == course_id)
                .where(LearningSourceRevision.status.in_(("stored", "indexed")))
            )
            if requested_ids:
                query = query.where(LearningSource.id.in_(requested_ids))
            rows = cast(
                list[tuple[LearningSource, LearningSourceRevision]],
                session.execute(query).all(),
            )
        if not rows:
            raise SourceNotReady("course generation requires stored source revisions")
        if requested_ids and {row[0].id for row in rows} != set(requested_ids):
            raise SourceNotReady("one or more requested sources are unavailable")
        return tuple(
            SourceProvenance(
                source_id=source.id,
                revision=revision.revision,
                content_hash=revision.content_hash,
            )
            for source, revision in sorted(rows, key=lambda row: row[0].id)
        )


def _job_outcome(job) -> ApplicationOutcomeV1:
    return ApplicationOutcomeV1(
        state="completed",
        aggregate=_aggregate(job.id, job.revision),
        result=_job_result(job),
    )


def _job_result(job) -> dict[str, object]:
    return {
        "job_id": job.id,
        "course_id": job.course_id,
        "state": job.state.value,
        "attempt_number": job.attempt_number,
        "revision": job.revision,
    }


def _aggregate(job_id: str, revision: int) -> AggregateRefV1:
    return AggregateRefV1(
        aggregate_type="course_factory_job",
        aggregate_id=job_id,
        revision=revision,
    )


def _rejected(code: str, message: str) -> ApplicationOutcomeV1:
    return ApplicationOutcomeV1(
        state="rejected",
        error=ToolErrorV1(code=code, message=message[:500] or code, retryable=False),
    )


def _required_course_id(request: ToolRequestV1) -> str:
    if request.event.course_id is None:
        raise ValueError("course_id is required")
    return str(request.event.course_id)


def _string(payload: Mapping[str, object], key: str, *, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} is invalid")
    return value.strip()


def _uuid(payload: Mapping[str, object], key: str) -> str:
    value = _string(payload, key, maximum=36)
    return str(UUID(value))


def _optional_uuid(payload: Mapping[str, object], key: str) -> str | None:
    return _uuid(payload, key) if key in payload else None


def _source_ids(payload: Mapping[str, object]) -> tuple[str, ...]:
    value = payload.get("source_ids", [])
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("source_ids is invalid")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("source_ids is invalid")
    result = tuple(str(UUID(item)) for item in value)
    if len(result) != len(set(result)):
        raise ValueError("source_ids is invalid")
    return result


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _generation_hash(
    *,
    course_id: str,
    request: GenerationRequest,
    provenance: tuple[SourceProvenance, ...],
) -> str:
    return generation_request_hash(course_id=course_id, request=request, provenance=provenance)


def _draft_summary(raw: object) -> dict[str, object] | None:
    """Outline of the verified draft, so the person sees what they approve."""
    try:
        draft = CourseDraft.model_validate(raw)
    except ValidationError:
        return None
    lesson_titles = {lesson.lesson_id: lesson.title for lesson in draft.lessons}
    return {
        "title": draft.title,
        "chapters": [
            {
                "title": chapter.title,
                "lessons": [
                    lesson_titles[lesson_id]
                    for lesson_id in chapter.lesson_ids
                    if lesson_id in lesson_titles
                ],
                "activity_count": len(chapter.activity_ids),
            }
            for chapter in draft.chapters
        ],
    }
