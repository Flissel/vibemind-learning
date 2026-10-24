"""Human review of held AI evaluations over HTTP (role ``learning.reviewer``).

``GET /reviews`` lists the open reviews, ``GET /reviews/{id}`` adds the drawing the AI
graded, ``POST /reviews/{id}/decision`` records a person's confirmation or new score.
The decision itself is made on the claw's local review page, never by a model.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from spaces.learning.deployment.vibemind_auth import (
    ROLE_REVIEWER,
    ApiError,
    VibeMindRequestContext,
    vibemind_context,
)
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.db.repository import PersistenceConflict

_ARTIFACT_SCHEME = "artifact://learning/"
_MAX_DRAWING_BYTES = 20_000_000


class ReviewDecisionRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["confirm", "override"]
    score: Annotated[float, Field(ge=0, le=1)] | None = None
    note: Annotated[str, Field(max_length=2_000)] | None = None


@dataclass(frozen=True)
class ReviewRuntime:
    service: AdaptiveSessionService | None
    artifact_root: Path | None


@lru_cache(maxsize=1)
def _default_runtime() -> ReviewRuntime:
    from spaces.learning.services.db.session import (
        build_session_factory,
        create_learning_engine,
    )

    database_url = os.environ.get("LEARNING_DATABASE_URL", "").strip()
    artifact_root = os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip()
    return ReviewRuntime(
        service=(
            AdaptiveSessionService(build_session_factory(create_learning_engine(database_url)))
            if database_url
            else None
        ),
        artifact_root=Path(artifact_root) if artifact_root else None,
    )


def get_review_runtime() -> ReviewRuntime:
    return _default_runtime()


Context = Annotated[VibeMindRequestContext, Depends(vibemind_context)]
Runtime = Annotated[ReviewRuntime, Depends(get_review_runtime)]

router = APIRouter(prefix="/api/v1")


@router.get("/reviews")
def list_reviews(context: Context, runtime: Runtime) -> JSONResponse:
    context.require(ROLE_REVIEWER)
    return JSONResponse({"reviews": _service(runtime).pending_reviews()})


@router.get("/reviews/{review_id}")
def read_review(review_id: UUID, context: Context, runtime: Runtime) -> JSONResponse:
    context.require(ROLE_REVIEWER)
    entry = _pending(runtime, str(review_id))
    drawing = _drawing(runtime, entry.get("drawing_locator"))
    return JSONResponse({
        **entry,
        "drawing_png_base64": base64.b64encode(drawing).decode() if drawing else None,
    })


@router.post("/reviews/{review_id}/decision")
def decide_review(
    review_id: UUID, body: ReviewDecisionRequestV1, context: Context, runtime: Runtime
) -> JSONResponse:
    context.require(ROLE_REVIEWER)
    try:
        result = _service(runtime).resolve_review(
            str(review_id),
            reviewer_id=context.actor_id,
            decision=body.decision,
            score=body.score,
            note=body.note,
        )
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Review not found") from error
    except PersistenceConflict as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, "review_decided", "Review is already decided"
        ) from error
    except ValueError as error:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", str(error)) from error
    return JSONResponse(result, status_code=status.HTTP_200_OK)


def _service(runtime: ReviewRuntime) -> AdaptiveSessionService:
    if runtime.service is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "dependency_unavailable",
            "Learning database is not configured",
            retryable=True,
        )
    return runtime.service


def _pending(runtime: ReviewRuntime, review_id: str) -> dict[str, object]:
    for entry in _service(runtime).pending_reviews(limit=200):
        if entry["review_id"] == review_id:
            return entry
    raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "No open review with this id")


def _drawing(runtime: ReviewRuntime, locator: object) -> bytes | None:
    """The graded snapshot, read only from inside the artifact root."""
    if runtime.artifact_root is None or not isinstance(locator, str):
        return None
    if not locator.startswith(_ARTIFACT_SCHEME):
        return None
    root = runtime.artifact_root.resolve()
    path = (root / locator[len(_ARTIFACT_SCHEME):]).resolve()
    if not path.is_relative_to(root) or path.suffix != ".png" or not path.is_file():
        return None
    if path.stat().st_size > _MAX_DRAWING_BYTES:
        return None
    return path.read_bytes()
