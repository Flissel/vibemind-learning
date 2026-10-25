"""The sequence player: LearnHouse pages embed ``/lernsequenz/<id>?k=<token>``.

The page and its data carry no solutions (``public_view``); answers are graded here.
Each link holds an HMAC token for exactly one sequence, derived from the Learning service
key, so a sequence cannot be guessed or enumerated. v1 counts answers for the local
learner; mapping the LearnHouse person comes with the player integration (stage 0b).
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Query, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from spaces.learning.contracts.lernsequenz import Choice, DrawnExplanation, public_view
from spaces.learning.deployment.vibemind_auth import ApiError
from spaces.learning.services.lernsequenz.bewertung import grade
from spaces.learning.services.lernsequenz.store import SequenceStore
from spaces.learning.services.lernsequenz.token import sequence_token
from spaces.learning.services.lernsequenz.tutor import tutor_context

# Below the worker's gateway limits (wrapper 540 s < gateway 600 s).
_TUTOR_TIMEOUT_S = 630.0
_PLAYER = Path(__file__).with_name("lernsequenz_player.html")
_MEDIA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(mp4|webm|mp3|png|jpg|webp)$")
ANCESTORS_ENV = "LEARNING_EMBED_ANCESTORS"
ACTOR_ENV = "LEARNING_SEQUENCE_ACTOR"
logger = logging.getLogger(__name__)

router = APIRouter()
Token = Annotated[str, Query(alias="k", min_length=16, max_length=128)]


@lru_cache(maxsize=1)
def _store() -> SequenceStore:
    root = os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip()
    if not root:
        raise RuntimeError("Learning artifact root is not configured")
    return SequenceStore(Path(root))


def _checked(sequence_id: str, token: str):
    try:
        expected = sequence_token(sequence_id)
    except RuntimeError as error:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", str(error)) from error
    if not hmac.compare_digest(expected, token):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Sequence not found")
    try:
        return _store().load(sequence_id)
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Sequence not found") from error


def _headers(nonce: str | None = None) -> dict[str, str]:
    ancestors = os.environ.get(ANCESTORS_ENV, "http://127.0.0.1:13000 http://localhost:13000").strip()
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            f"default-src 'none'; script-src {script}; style-src 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self' https:; connect-src 'self'; "
            f"frame-ancestors 'self' {ancestors}; base-uri 'none'; form-action 'none'"
        ),
    }


@router.get("/lernsequenz/{sequence_id}", response_class=HTMLResponse)
def player(sequence_id: str, k: Token) -> HTMLResponse:
    _checked(sequence_id, k)
    nonce = secrets.token_urlsafe(16)
    page = _PLAYER.read_text(encoding="utf-8").replace("__NONCE__", nonce)
    return HTMLResponse(page, headers=_headers(nonce))


@router.get("/lernsequenz/{sequence_id}/daten")
def data(sequence_id: str, k: Token) -> JSONResponse:
    sequence = _checked(sequence_id, k)
    return JSONResponse(public_view(sequence), headers=_headers())


@router.post("/lernsequenz/{sequence_id}/antwort")
def answer(
    sequence_id: str,
    k: Token,
    payload: Annotated[dict[str, Any], Body()],
) -> JSONResponse:
    sequence = _checked(sequence_id, k)
    checkpoint_id = payload.get("checkpoint_id")
    reply = payload.get("answer")
    if not isinstance(checkpoint_id, str) or not isinstance(reply, dict):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "checkpoint_id and answer are required")
    try:
        checkpoint = sequence.checkpoint(checkpoint_id)
        if isinstance(checkpoint, DrawnExplanation):
            sketch_id = _store().save_sketch(sequence, checkpoint_id, reply.get("strokes"))
            result: dict[str, Any] = {"kind": "drawn_explanation", "saved": True, "sketch_id": sketch_id}
        else:
            result = grade(sequence, checkpoint_id, reply)
            if isinstance(checkpoint, Choice) and checkpoint.activity_id:
                result["mastery"] = _count_for_mastery(sequence.course_id, checkpoint.activity_id, reply)
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Checkpoint not found") from error
    except ValueError as error:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", str(error)) from error
    return JSONResponse(result, headers=_headers())


@lru_cache(maxsize=1)
def _sessions():
    from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
    from spaces.learning.services.db.session import build_session_factory, create_learning_engine

    url = os.environ.get("LEARNING_DATABASE_URL", "").strip()
    return AdaptiveSessionService(build_session_factory(create_learning_engine(url))) if url else None


def _count_for_mastery(course_id: str, activity_id: str, reply: dict[str, Any]) -> dict[str, Any]:
    """Book the answer for the local learner (same identity as the claw practice, v1).

    Mapping the LearnHouse person comes with stage 0b; a booking failure never hides
    the answer's feedback.
    """
    service = _sessions()
    if service is None:
        return {"counted": False}
    try:
        booked = service.record_sequence_answer(
            actor_id=os.environ.get(ACTOR_ENV, "learning-claw"),
            course_id=course_id,
            activity_id=activity_id,
            selected=[s for s in reply.get("selected", []) if isinstance(s, str)],
        )
    except Exception:
        logger.exception("lernsequenz_mastery_booking_failed")
        return {"counted": False}
    return {"counted": booked is not None, "mastery_applied": bool(booked and booked["mastery_applied"])}


@router.post("/lernsequenz/{sequence_id}/tutor")
async def tutor(
    sequence_id: str,
    k: Token,
    payload: Annotated[dict[str, Any], Body()],
) -> JSONResponse:
    """Ask the tutor about the current slide or checkpoint; the model runs in learning-worker."""
    sequence = _checked(sequence_id, k)
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > 2_000:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "question is required")
    section_id, checkpoint_id = payload.get("section_id"), payload.get("checkpoint_id")
    try:
        task, sources = tutor_context(
            sequence,
            section_id=section_id if isinstance(section_id, str) else None,
            checkpoint_id=checkpoint_id if isinstance(checkpoint_id, str) else None,
        )
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", str(error)) from error
    url = os.environ.get("LEARNING_EVALUATION_SERVICE_URL", "").rstrip("/")
    key = os.environ.get("LEARNING_EVALUATION_SERVICE_KEY", "")
    if not url or len(key) < 32:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Tutor is not configured", retryable=True)
    try:
        async with httpx.AsyncClient(timeout=_TUTOR_TIMEOUT_S) as client:
            response = await client.post(
                f"{url}/internal/sequence/tutor",
                headers={"X-VibeMind-Learning-Evaluation-Key": key},
                json={"question": question, "task": task, "sources": sources, "correlation_id": str(uuid4())},
            )
    except httpx.HTTPError as error:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Tutor is unavailable", retryable=True) from error
    if response.status_code != 200:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Tutor gave no grounded answer", retryable=True)
    answer = response.json()["tutor"]
    texts = {source["key"]: source["source"] for source in sources}
    return JSONResponse({
        "answer": answer["answer"],
        "next_step": answer["next_step"],
        "cited": [texts[k_][:160] for k_ in answer["source_keys"] if k_ in texts],
    }, headers=_headers())


@router.get("/lernsequenz-medien/{name}")
def media(name: str) -> Response:
    """Videos and images of sequences; names only, no paths."""
    if not _MEDIA_NAME.fullmatch(name):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Media not found")
    root = os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip()
    path = (Path(root) / "lernsequenzen" / "medien" / name) if root else None
    if path is None or not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Media not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600",
                                       "X-Content-Type-Options": "nosniff"})
