from __future__ import annotations

import base64
import binascii
import hmac
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import sessionmaker

from spaces.learning.contracts.penecho import CanvasSubmissionV1, SourceReferenceV1
from spaces.learning.mcp.tools.canvas import CanvasAdaptiveSubmissionService
from spaces.learning.mcp.tools.tutor import TutorService
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.course_factory.model_gateway import (
    ModelGatewayError,
    build_openfang_model_gateway,
    png_bild,
)
from spaces.learning.services.db.session import create_learning_engine
from spaces.learning.services.evaluation.handschrift import HandschriftLeser
from spaces.learning.services.evaluation.schemas import RubricDecision, RubricEvaluationRequest
from spaces.learning.services.evaluation.session_rubric import SessionRubricEvaluator
from spaces.learning.services.lernsequenz.tutor import ask_explain, ask_tutor


KEY_HEADER = "X-VibeMind-Learning-Evaluation-Key"
KEY_ENV = "LEARNING_EVALUATION_SERVICE_KEY"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CanvasSubmitRequest(_StrictModel):
    session_id: UUID
    actor_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    expected_session_revision: Annotated[int, Field(ge=1)]
    submission_id: UUID
    submission: CanvasSubmissionV1
    snapshot_png_base64: Annotated[str, Field(min_length=12, max_length=27_000_000)]
    source_refs: Annotated[tuple[SourceReferenceV1, ...], Field(min_length=1, max_length=256)]


class CanvasHintRequest(_StrictModel):
    session_id: UUID
    actor_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    question: Annotated[str, Field(min_length=1, max_length=10_000)]
    correlation_id: UUID
    # The learner's current drawing (canvas hint); the tutor sees it as an image.
    drawing_png_base64: Annotated[str, Field(min_length=12, max_length=27_000_000)] | None = None


# A PNG is at most 5 MB (model gateway); base64 makes that 4/3 of it. Checked before decoding.
_MAX_PNG_BASE64 = 6_700_000


class HandschriftLesenRequest(_StrictModel):
    task: dict[str, object]
    png_base64: Annotated[str, Field(min_length=12, max_length=_MAX_PNG_BASE64)]


class RubricEvaluateRequest(RubricEvaluationRequest):
    # The learner's handwritten result area, for open, case and canvas tasks.
    bild_png_base64: Annotated[str, Field(min_length=12, max_length=_MAX_PNG_BASE64)] | None = None


class SequenceTutorRequest(_StrictModel):
    question: Annotated[str, Field(min_length=1, max_length=2_000)]
    task: dict[str, object]
    sources: Annotated[tuple[dict[str, str], ...], Field(min_length=1, max_length=12)]
    correlation_id: UUID


class SequenceExplainRequest(_StrictModel):
    task: dict[str, object]
    sources: Annotated[tuple[dict[str, str], ...], Field(min_length=1, max_length=12)]
    correlation_id: UUID
    # A video frame of the marked place, when the player sends one.
    image_png_base64: Annotated[str, Field(max_length=4_000_000)] | None = None


@dataclass(frozen=True)
class _EvaluationServices:
    canvas: CanvasAdaptiveSubmissionService
    tutor: TutorService
    rubric: SessionRubricEvaluator
    leser: HandschriftLeser


@lru_cache(maxsize=1)
def _services() -> _EvaluationServices:
    database_url = os.environ.get("LEARNING_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("canvas evaluation database is not configured")
    engine = create_learning_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    gateway = build_openfang_model_gateway()
    return _EvaluationServices(
        canvas=CanvasAdaptiveSubmissionService(
            session_factory=factory,
            # A fresh gateway per drawing: each submission runs its own event loop.
            gateway=build_openfang_model_gateway,
        ),
        tutor=TutorService(
            sessions=AdaptiveSessionService(
                factory, external_rubric_types=frozenset({"penecho_canvas"})
            ),
            gateway=gateway,
        ),
        rubric=SessionRubricEvaluator(build_openfang_model_gateway),
        leser=HandschriftLeser(build_openfang_model_gateway),
    )


def _require_key(supplied: str) -> None:
    configured = os.environ.get(KEY_ENV, "")
    if len(configured) < 32:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "evaluation service is not configured")
    if not hmac.compare_digest(supplied.encode(), configured.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid evaluation credentials")


app = FastAPI(title="VibeMind Learning Evaluation", version="1.0.0")


@app.get("/health/ready")
def ready() -> dict[str, str]:
    _services()
    return {"status": "ok"}


@app.post("/internal/canvas/submit")
def submit_canvas(
    payload: CanvasSubmitRequest,
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, object]:
    _require_key(key)
    try:
        snapshot = base64.b64decode(payload.snapshot_png_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid canvas snapshot") from error
    outcome = _services().canvas.submit(
        session_id=str(payload.session_id), actor_id=payload.actor_id,
        expected_session_revision=payload.expected_session_revision,
        submission_id=str(payload.submission_id), submission=payload.submission,
        snapshot_png=snapshot, source_refs=payload.source_refs,
    )
    return {
        "result": outcome.result.model_dump(mode="json"),
        "turn": outcome.turn.model_dump(mode="json"),
    }


@app.post("/internal/canvas/hint")
async def hint_canvas(
    payload: CanvasHintRequest,
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, object]:
    _require_key(key)
    drawing = None
    if payload.drawing_png_base64 is not None:
        try:
            drawing = base64.b64decode(payload.drawing_png_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid drawing") from error
    answer = await _services().tutor.ask(
        session_id=str(payload.session_id), actor_id=payload.actor_id,
        question=payload.question, correlation_id=str(payload.correlation_id),
        drawing_png=drawing,
    )
    return {"tutor": answer.model_dump(mode="json")}


@app.post("/internal/sequence/tutor")
async def sequence_tutor(
    payload: SequenceTutorRequest,
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, object]:
    """Tutor for the sequence player; context and sources are built by learning-api."""
    _require_key(key)
    try:
        answer = await ask_tutor(
            build_openfang_model_gateway(),
            question=payload.question,
            task=payload.task,
            sources=[dict(source) for source in payload.sources],
            correlation_id=str(payload.correlation_id),
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    return {"tutor": answer}


@app.post("/internal/sequence/erklaeren")
async def sequence_explain(
    payload: SequenceExplainRequest,
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, object]:
    """Explain a marked place of a sequence; context and sources are built by learning-api."""
    _require_key(key)
    image: bytes | None = None
    if payload.image_png_base64:
        try:
            image = base64.b64decode(payload.image_png_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid image") from error
    try:
        answer = await ask_explain(
            build_openfang_model_gateway(),
            task=payload.task,
            sources=[dict(source) for source in payload.sources],
            correlation_id=str(payload.correlation_id),
            image_png=image,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    return {"explanation": answer}


@app.post("/internal/rubric/evaluate")
def evaluate_rubric(
    payload: RubricEvaluateRequest,
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, object]:
    _require_key(key)
    request = RubricEvaluationRequest.model_validate(
        payload.model_dump(exclude={"bild_png_base64"})
    )
    services = _services()
    if payload.bild_png_base64 is None:
        decision: RubricDecision = services.rubric.evaluate(request)
    else:
        bild = _bild(payload.bild_png_base64)
        decision = services.rubric.evaluate(request, bild=bild)
    return {"decision": decision.model_dump(mode="json")}


def _bild(png_base64: str):
    try:
        return png_bild(base64.b64decode(png_base64, validate=True))
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid handwriting image") from error


@app.post("/internal/handschrift/lesen")
def handschrift_lesen(
    payload: HandschriftLesenRequest,
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, object]:
    """Reads the learner's handwriting; a model error is an evaluation error, never a reading."""
    _require_key(key)
    png = _bild(payload.png_base64).content
    try:
        gelesen, antwort = _services().leser.lesen(payload.task, png)
    except ValueError as error:  # includes GatewayInputError
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    except ModelGatewayError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "handwriting reading failed") from error
    return {"gelesen": gelesen.model_dump(mode="json"), "antwort": antwort}


@app.get("/internal/evaluations/{evaluation_id}")
def evaluation_exists(
    evaluation_id: UUID,
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, bool]:
    _require_key(key)
    return {"exists": _services().canvas.has_evaluation(str(evaluation_id))}


@app.get("/internal/evaluations/{evaluation_id}/review")
def evaluation_review(
    evaluation_id: UUID,
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, object]:
    _require_key(key)
    return {"review": _services().canvas.review(str(evaluation_id))}


@app.get("/internal/sessions/{session_id}/revisions/{revision}")
def session_revision_exists(
    session_id: UUID,
    revision: Annotated[int, Field(ge=1)],
    key: Annotated[str, Header(alias=KEY_HEADER, max_length=512)] = "",
) -> dict[str, bool]:
    _require_key(key)
    return {
        "exists": _services().canvas.has_session_revision(str(session_id), revision)
    }


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8092, log_level="warning")
