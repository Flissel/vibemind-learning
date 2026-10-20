"""Authenticated HTTP adapter for the Learning Course Factory.

Every route validates a typed request, builds one ``EventEnvelopeV1`` and
submits it through the durable Learning dispatcher -- the same path the MCP
tools use. Receipts, revisions, review, publication, and readback are
therefore identical across transports; this module owns no lifecycle logic.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from spaces.learning.bridge.dispatcher import LearningDispatcher
from spaces.learning.bridge.learnhouse_client import STAGED_ARTIFACT_SCHEME
from spaces.learning.contracts.course_factory_api import (
    CourseGenerationRequestV1,
    CourseProjectionResponseV1,
    FactoryJobResponseV1,
    MaterialImportRequestV1,
    MaterialImportResponseV1,
    MaterialStateResponseV1,
    PublishRequestV1,
    ReviewRequestV1,
)
from spaces.learning.contracts.events import EVENT_TOOL_MAP, LearningEventType
from spaces.learning.contracts.mcp_models import (
    ActorV1,
    ConfirmationV1,
    EventEnvelopeV1,
    ToolRequestV1,
)
from spaces.learning.contracts.outcomes import ToolResultV1
from spaces.learning.deployment.vibemind_auth import (
    ROLE_AUTHOR,
    ROLE_PUBLISHER,
    ROLE_REVIEWER,
    ApiError,
    VibeMindRequestContext,
    vibemind_context,
)
from spaces.learning.services.ingestion.material_staging import (
    MaterialStageConflict,
    MaterialStageRejected,
    MaterialStagingService,
)
from spaces.learning.services.ingestion.models import (
    LearningSource,
    LearningSourceRevision,
)


SessionFactory = Callable[[], Session]
IMPORT_ROOT_ENV = "LEARNING_IMPORT_ROOT"

# Stable namespace so a replayed (actor, idempotency key) stages one artifact.
_MATERIAL_NAMESPACE = UUID("8d0c6a52-4f3e-4d6b-9a37-3f1f0f6c2e11")

# Literal 422: the Starlette constant was renamed between releases.
_UNPROCESSABLE = 422

_STATUS_BY_CODE = {
    "idempotency_conflict": status.HTTP_409_CONFLICT,
    "idempotency_in_progress": status.HTTP_409_CONFLICT,
    "revision_conflict": status.HTTP_409_CONFLICT,
    "not_found": status.HTTP_404_NOT_FOUND,
}


@dataclass(frozen=True)
class CourseFactoryRuntime:
    dispatcher: LearningDispatcher
    session_factory: SessionFactory | None
    stager: MaterialStagingService | None


@lru_cache(maxsize=1)
def _default_runtime() -> CourseFactoryRuntime:
    from spaces.learning.mcp.server import default_dispatcher
    from spaces.learning.services.db.session import (
        build_session_factory,
        create_learning_engine,
    )

    database_url = os.environ.get("LEARNING_DATABASE_URL", "").strip()
    artifact_root = os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip()
    import_root = os.environ.get(IMPORT_ROOT_ENV, "").strip()
    session_factory = (
        build_session_factory(create_learning_engine(database_url)) if database_url else None
    )
    stager = None
    if session_factory is not None and artifact_root and import_root:
        if Path(import_root).is_dir() and Path(artifact_root).is_dir():
            stager = MaterialStagingService(
                session_factory=session_factory,
                import_root=Path(import_root),
                artifact_root=Path(artifact_root),
            )
    return CourseFactoryRuntime(
        dispatcher=default_dispatcher(),
        session_factory=session_factory,
        stager=stager,
    )


def get_course_factory_runtime() -> CourseFactoryRuntime:
    return _default_runtime()


Context = Annotated[VibeMindRequestContext, Depends(vibemind_context)]
Runtime = Annotated[CourseFactoryRuntime, Depends(get_course_factory_runtime)]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", max_length=128)]

router = APIRouter(prefix="/api/v1")


@router.post("/course-factory/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_job(
    body: CourseGenerationRequestV1,
    context: Context,
    runtime: Runtime,
    idempotency_key: IdempotencyHeader = "",
) -> JSONResponse:
    context.require(ROLE_AUTHOR)
    result = _dispatch(
        runtime,
        context,
        LearningEventType.COURSE_GENERATE,
        idempotency_key=_required_key(idempotency_key),
        course_id=body.course_id,
        expected_revision=body.expected_course_revision,
        payload={
            "audience": body.audience,
            "target_outcome": _target_outcome(body),
            "source_ids": [str(item) for item in body.source_ids],
        },
    )
    return _job_response(result, context, status.HTTP_202_ACCEPTED)


@router.get("/course-factory/jobs/{job_id}")
def read_job(job_id: UUID, context: Context, runtime: Runtime) -> JSONResponse:
    return _job_response(_status(runtime, context, job_id), context, status.HTTP_200_OK)


@router.post("/course-factory/jobs/{job_id}/review")
def review_job(
    job_id: UUID,
    body: ReviewRequestV1,
    context: Context,
    runtime: Runtime,
    idempotency_key: IdempotencyHeader = "",
) -> JSONResponse:
    context.require(ROLE_REVIEWER)
    key = _required_key(idempotency_key)
    current = _status(runtime, context, job_id)
    payload: dict[str, Any] = {
        "job_id": str(job_id),
        "review": "approved" if body.decision == "approve" else "rejected",
    }
    if body.reason is not None:
        payload["reason"] = body.reason
    result = _dispatch(
        runtime,
        context,
        LearningEventType.COURSE_REVIEW,
        idempotency_key=key,
        course_id=UUID(_text(current, "course_id")),
        expected_revision=body.expected_revision,
        payload=payload,
    )
    return _job_response(result, context, status.HTTP_200_OK, base=current)


@router.post("/course-factory/jobs/{job_id}/publish")
def publish_job(
    job_id: UUID,
    body: PublishRequestV1,
    context: Context,
    runtime: Runtime,
    idempotency_key: IdempotencyHeader = "",
) -> JSONResponse:
    context.require(ROLE_PUBLISHER)
    key = _required_key(idempotency_key)
    current = _status(runtime, context, job_id)
    result = _dispatch(
        runtime,
        context,
        LearningEventType.COURSE_PUBLISH,
        idempotency_key=key,
        course_id=UUID(_text(current, "course_id")),
        expected_revision=body.expected_revision,
        confirmation=ConfirmationV1(confirmed=True, approval_ref=body.approval_ref),
        payload={"job_id": str(job_id)},
    )
    return _job_response(result, context, status.HTTP_200_OK, base=current)


@router.get("/courses/{course_id}")
def read_course(course_id: UUID, context: Context, runtime: Runtime) -> JSONResponse:
    result = _completed(
        _dispatch(runtime, context, LearningEventType.COURSE_OPEN, course_id=course_id),
        context,
    )
    assert result.aggregate is not None
    response = CourseProjectionResponseV1(
        correlation_id=context.correlation_id,
        outcome=result.state,
        course_id=str(course_id),
        revision=result.aggregate.revision,
        course=dict(result.result or {}),
        evidence=result.evidence,
    )
    return JSONResponse(response.model_dump(mode="json"), status_code=status.HTTP_200_OK)


def _source_url(body: MaterialImportRequestV1, artifact_id: str) -> str:
    """Provenance LearnHouse records for the source: the web URL, else the staged artifact."""
    if body.source.kind == "https":
        return str(body.source.url)
    return f"{STAGED_ARTIFACT_SCHEME}://{artifact_id}"


@router.post("/materials/import", status_code=status.HTTP_202_ACCEPTED)
def import_material(
    body: MaterialImportRequestV1,
    context: Context,
    runtime: Runtime,
    idempotency_key: IdempotencyHeader = "",
) -> JSONResponse:
    context.require(ROLE_AUTHOR)
    key = _required_key(idempotency_key)
    if runtime.stager is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "dependency_unavailable",
            "Learning material import is not configured",
            retryable=True,
        )
    artifact_id = str(uuid5(_MATERIAL_NAMESPACE, f"{context.actor_id}\x00{key}"))
    try:
        staged = runtime.stager.stage(body.source, artifact_id=artifact_id)
    except MaterialStageConflict as error:
        raise ApiError(status.HTTP_409_CONFLICT, "idempotency_conflict", str(error)) from error
    except MaterialStageRejected as error:
        raise ApiError(
            _UNPROCESSABLE, "material_rejected", str(error)
        ) from error
    result = _completed(
        _dispatch(
            runtime,
            context,
            LearningEventType.MATERIAL_IMPORT,
            idempotency_key=key,
            course_id=body.course_id,
            expected_revision=body.expected_course_revision,
            payload={
                "artifact_id": staged.artifact_id,
                "title": body.title,
                "source_url": _source_url(body, staged.artifact_id),
            },
        ),
        context,
    )
    data = result.result or {}
    source = data.get("source")
    if not isinstance(source, dict):
        raise _unverified(context)
    response = MaterialImportResponseV1(
        correlation_id=context.correlation_id,
        outcome=result.state,
        source_id=_text(source, "source_id"),
        revision=int(source.get("revision", 0)),
        artifact_id=staged.artifact_id,
        projection_state=_projection(data.get("projection_state")),
        content_hash=staged.content_hash,
        locators=tuple(
            item for item in source.get("locators", []) if isinstance(item, str)
        ),
        evidence=result.evidence,
    )
    return JSONResponse(
        response.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED
    )


@router.get("/materials/{source_id}")
def read_material(source_id: UUID, context: Context, runtime: Runtime) -> JSONResponse:
    if runtime.session_factory is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "dependency_unavailable",
            "Learning database is not configured",
            retryable=True,
        )
    with runtime.session_factory() as session:
        source = session.get(LearningSource, str(source_id))
        revision = (
            session.get(LearningSourceRevision, (source.id, source.current_revision))
            if source is not None
            else None
        )
    if source is None or revision is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Learning source not found")
    response = MaterialStateResponseV1(
        source_id=source.id,
        course_id=source.course_id,
        revision=revision.revision,
        content_hash=revision.content_hash,
        projection_state=(
            "complete"
            if revision.status == "indexed"
            else "failed" if revision.status == "failed" else "pending"
        ),
    )
    return JSONResponse(response.model_dump(mode="json"), status_code=status.HTTP_200_OK)


def _dispatch(
    runtime: CourseFactoryRuntime,
    context: VibeMindRequestContext,
    event_type: LearningEventType,
    *,
    idempotency_key: str | None = None,
    course_id: UUID | None = None,
    expected_revision: int | None = None,
    confirmation: ConfirmationV1 | None = None,
    payload: dict[str, Any] | None = None,
) -> ToolResultV1:
    try:
        event = EventEnvelopeV1(
            event_type=event_type,
            invocation_id=uuid4(),
            correlation_id=context.correlation_id,
            actor=ActorV1(actor_id=context.actor_id, actor_type="local_user"),
            course_id=course_id,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            confirmation=confirmation,
            payload=payload or {},
        )
        request = ToolRequestV1(tool=EVENT_TOOL_MAP[event_type], event=event)
    except ValidationError as error:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST, "invalid_request", "Request validation failed"
        ) from error
    return runtime.dispatcher.dispatch(request)


def _status(
    runtime: CourseFactoryRuntime, context: VibeMindRequestContext, job_id: UUID
) -> ToolResultV1:
    return _completed(
        _dispatch(
            runtime,
            context,
            LearningEventType.GENERATION_STATUS,
            payload={"job_id": str(job_id)},
        ),
        context,
    )


def _completed(result: ToolResultV1, context: VibeMindRequestContext) -> ToolResultV1:
    """Return a terminal success (or approval request) or raise its public error."""
    if result.state in {"completed", "approval_required"}:
        return result
    if result.state == "accepted":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "idempotency_in_progress",
            "An identical request is still in progress",
            retryable=True,
        )
    error = result.error
    code = error.code if error is not None else "dependency_unavailable"
    message = error.message if error is not None else "Learning backend is unavailable"
    if result.state == "unavailable":
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, code, message, retryable=True
        )
    raise ApiError(
        _STATUS_BY_CODE.get(code, _UNPROCESSABLE),
        code,
        message,
        retryable=bool(error and error.retryable),
    )


def _job_response(
    result: ToolResultV1,
    context: VibeMindRequestContext,
    status_code: int,
    *,
    base: ToolResultV1 | None = None,
) -> JSONResponse:
    result = _completed(result, context)
    if result.aggregate is None:
        raise _unverified(context)
    data: dict[str, Any] = dict(base.result or {}) if base is not None else {}
    data.update(result.result or {})
    unsupported = data.get("unsupported_claim_ids", [])
    response = FactoryJobResponseV1(
        correlation_id=context.correlation_id,
        outcome=result.state,
        job_id=result.aggregate.aggregate_id,
        course_id=_text(data, "course_id"),
        state=_text(data, "state"),
        attempt_number=int(data.get("attempt_number", 0)),
        revision=result.aggregate.revision,
        publish_confirmation_required=bool(data.get("publish_confirmation_required", False)),
        unsupported_claim_ids=tuple(unsupported) if isinstance(unsupported, list) else (),
        draft_summary=data.get("draft_summary"),
        quality_issue_codes=data.get("quality_issue_codes"),
        ai_review_issues=data.get("ai_review_issues"),
        evidence=result.evidence,
    )
    return JSONResponse(response.model_dump(mode="json"), status_code=status_code)


def _required_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_request",
            "Mutating requests require an Idempotency-Key header",
        )
    return key


def _target_outcome(body: CourseGenerationRequestV1) -> str:
    try:
        return body.target_outcome()
    except ValueError as error:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", str(error)) from error


def _text(data: Any, key: str) -> str:
    value = data.get(key) if isinstance(data, dict) else None
    if value is None and isinstance(data, ToolResultV1):
        value = (data.result or {}).get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "truth_readback_unverified",
            "Learning readback is incomplete",
            retryable=True,
        )
    return value


def _projection(value: object) -> str:
    return value if value in {"pending", "complete", "failed"} else "pending"


def _unverified(context: VibeMindRequestContext) -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "truth_readback_unverified",
        "Learning readback is incomplete",
        retryable=True,
    )
