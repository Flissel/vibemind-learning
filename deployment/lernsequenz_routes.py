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
from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from spaces.learning.contracts.lernsequenz import Choice, DrawnExplanation, public_view
from spaces.learning.deployment.vibemind_auth import (
    ROLE_AUTHOR,
    ApiError,
    VibeMindRequestContext,
    vibemind_context,
)
from spaces.learning.services.lernsequenz.aus_kurs import as_video
from spaces.learning.services.lernsequenz.bewertung import grade
from spaces.learning.services.lernsequenz.store import SequenceStore
from spaces.learning.services.lernsequenz.token import sequence_token
from spaces.learning.services.lernsequenz.tutor import explain_context, tutor_context
from spaces.learning.services.lernsequenz.zahlen import format_number, read_number, why_not_a_number

# Below the worker's gateway limits (wrapper 540 s < gateway 600 s).
_TUTOR_TIMEOUT_S = 630.0
_PLAYER = Path(__file__).with_name("lernsequenz_player.html")
_MEDIA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(mp4|webm|mp3|png|jpg|webp)$")
ANCESTORS_ENV = "LEARNING_EMBED_ANCESTORS"
ACTOR_ENV = "LEARNING_SEQUENCE_ACTOR"
logger = logging.getLogger(__name__)

router = APIRouter()
Token = Annotated[str, Query(alias="k", min_length=16, max_length=128)]
# The signed-in person, as LearnHouse signed it into the embed link (services/lernsequenz/person.py):
# the header is what the player sends; the query form is what the embed link carries.
Person = Annotated[str | None, Query(alias="p", max_length=512)]
PersonHeader = Annotated[str | None, Header(alias="X-Learning-Person", max_length=512)]


def _actor(person: str | None) -> str:
    """The learner answers are booked on: the person from the token, else the shared default."""
    if not person:
        return os.environ.get(ACTOR_ENV, "learning-claw")
    from spaces.learning.services.lernsequenz.person import read_person_token

    try:
        return read_person_token(person)
    except LookupError as error:
        if str(error) == "expired":
            raise ApiError(status.HTTP_401_UNAUTHORIZED, "person_expired", "Bitte die Seite neu laden") from error
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "person_invalid", "Person token is not valid") from error
    except RuntimeError as error:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", str(error)) from error


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


@router.post("/lernsequenz/{sequence_id}/lesen")
def reading(sequence_id: str, k: Token, payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    """How a typed number is read, so the person sees the reading before it is graded."""
    _checked(sequence_id, k)
    text = payload.get("text")
    if not isinstance(text, str) or len(text) > 60:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "text is required")
    read = read_number(text)
    if read is None:
        return JSONResponse({"value": None, "display": None, "ambiguous": False, "alternative": None,
                             "percent": False, "reason": why_not_a_number(text)}, headers=_headers())
    alternative = (
        {"value": read.alternative, "display": format_number(read.alternative)}
        if read.alternative is not None else None
    )
    return JSONResponse({"value": read.value, "display": read.display, "ambiguous": read.ambiguous,
                         "alternative": alternative, "percent": read.percent, "reason": None}, headers=_headers())


@router.post("/lernsequenz/{sequence_id}/antwort")
def answer(
    sequence_id: str,
    k: Token,
    payload: Annotated[dict[str, Any], Body()],
    p: Person = None,
    person_header: PersonHeader = None,
) -> JSONResponse:
    sequence = _checked(sequence_id, k)
    actor_id = _actor(person_header or p)
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
                result["mastery"] = _count_for_mastery(sequence.course_id, checkpoint.activity_id, reply, actor_id=actor_id)
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


def _count_for_mastery(course_id: str, activity_id: str, reply: dict[str, Any], *, actor_id: str) -> dict[str, Any]:
    """Book the answer on the person (the shared default learner without a person token).

    A booking failure never hides the answer's feedback.
    """
    service = _sessions()
    if service is None:
        return {"counted": False}
    try:
        booked = service.record_sequence_answer(
            actor_id=actor_id,
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


_MAX_UPLOAD_BYTES = 1_500_000_000
Context = Annotated[VibeMindRequestContext, Depends(vibemind_context)]


_MAX_IMAGE_B64 = 4_000_000


async def _explain(sequence: LernsequenzV1, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The worker's explanation of a marked place: (raw answer, what the player gets)."""
    anchor, selection = payload.get("anchor"), payload.get("selection")
    if not isinstance(anchor, dict) or not isinstance(selection, str) or not selection.strip():
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "anchor and selection are required")
    image = payload.get("image_png_base64")
    if image is not None and (not isinstance(image, str) or len(image) > _MAX_IMAGE_B64):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "image is not usable")
    try:
        task, sources = explain_context(sequence, anchor=anchor, selection=selection)
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", str(error)) from error
    except ValueError as error:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", str(error)) from error
    url = os.environ.get("LEARNING_EVALUATION_SERVICE_URL", "").rstrip("/")
    key = os.environ.get("LEARNING_EVALUATION_SERVICE_KEY", "")
    if not url or len(key) < 32:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Tutor is not configured", retryable=True)
    body: dict[str, Any] = {"task": task, "sources": sources, "correlation_id": str(uuid4())}
    if image:
        body["image_png_base64"] = image
    try:
        async with httpx.AsyncClient(timeout=_TUTOR_TIMEOUT_S) as client:
            response = await client.post(
                f"{url}/internal/sequence/erklaeren", headers={"X-VibeMind-Learning-Evaluation-Key": key}, json=body,
            )
    except httpx.HTTPError as error:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Tutor is unavailable", retryable=True) from error
    if response.status_code != 200:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Tutor gave no grounded answer", retryable=True)
    answer = response.json().get("explanation") or {}
    keys = set(answer.get("source_keys") or [])
    return answer, {
        "explanation": answer.get("explanation", ""),
        "cited": [source["source"] for source in sources if source["key"] in keys],
        "extra": answer.get("extra", ""),
        "extra_is_outside_course": bool(answer.get("extra")),
    }


@router.post("/lernsequenz/{sequence_id}/erklaeren")
async def explain(
    sequence_id: str,
    k: Token,
    payload: Annotated[dict[str, Any], Body()],
    p: Person = None,
    person_header: PersonHeader = None,
) -> JSONResponse:
    """"Erklär mir das": the marked place, explained from the course by the worker."""
    sequence = _checked(sequence_id, k)
    _actor(person_header or p)
    _answer, body = await _explain(sequence, payload)
    return JSONResponse(body, headers=_headers())


@router.post("/lernsequenz/{sequence_id}/notizen")
async def note(
    sequence_id: str,
    k: Token,
    payload: Annotated[dict[str, Any], Body()],
    p: Person = None,
    person_header: PersonHeader = None,
) -> JSONResponse:
    """"Notiz anlegen": the explanation of a marked place, kept for the signed-in person."""
    sequence = _checked(sequence_id, k)
    if not (person_header or p):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "person_required", "Notizen gibt es nur angemeldet")
    actor_id = _actor(person_header or p)
    answer, body = await _explain(sequence, payload)
    from spaces.learning.deployment.notizen_routes import _store as note_store

    try:
        saved = note_store().create(
            actor_id=actor_id, course_id=sequence.course_id, sequence_id=sequence.sequence_id,
            anchor=payload["anchor"], selection=" ".join(payload["selection"].split()),
            explanation=answer.get("explanation", ""), extra=answer.get("extra", ""),
            extra_sources=[s for s in answer.get("extra_sources", []) if isinstance(s, str)],
        )
    except ValueError as error:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", str(error)) from error
    return JSONResponse({"note_id": saved["id"], **body}, headers=_headers())


def _media_root() -> Path:
    root = os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip()
    if not root:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Artifact root is not configured")
    return Path(root) / "lernsequenzen" / "medien"


@router.put("/api/v1/lernsequenz-medien/{name}")
async def upload_media(name: str, request: Request, context: Context) -> JSONResponse:
    """Slide images for Laura and the finished videos; names only, written atomically."""
    context.require(ROLE_AUTHOR)
    if not _MEDIA_NAME.fullmatch(name):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "Media name is not allowed")
    folder = _media_root()
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / f".{name}.{uuid4().hex}.part"
    size = 0
    try:
        with partial.open("wb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise ApiError(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too_large", "Media file is too large")
                stream.write(chunk)
        if size == 0:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "Media file is empty")
        os.replace(partial, folder / name)
    finally:
        partial.unlink(missing_ok=True)
    return JSONResponse({"name": name, "bytes": size, "url": f"/lernsequenz-medien/{name}"})


@router.post("/api/v1/lernsequenzen/{sequence_id}/video")
def register_video(sequence_id: str, context: Context, payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    """The presentation's explainer video from uploaded media and per-slide times."""
    context.require(ROLE_AUTHOR)
    media_name, times = payload.get("media_name"), payload.get("slide_times")
    if not isinstance(media_name, str) or not _MEDIA_NAME.fullmatch(media_name) or not isinstance(times, dict):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "media_name and slide_times are required")
    if not (_media_root() / media_name).is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Upload the video first")
    try:
        video = as_video(_store().load(sequence_id), media_name=media_name, slide_times=times)
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Sequence not found") from error
    except (ValueError, KeyError, TypeError) as error:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", str(error)) from error
    _store().save(video)
    return JSONResponse({"sequence_id": video.sequence_id, "checkpoints": len(video.checkpoints),
                         "link": _public_link(video.sequence_id)})


@router.post("/api/v1/lernsequenzen/einbetten")
async def embed_course(context: Context, payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    """What publishing does: presentation (and video) into the course's first lesson."""
    context.require(ROLE_AUTHOR)
    course_id = payload.get("course_id")
    if not isinstance(course_id, str) or not course_id:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "course_id is required")
    from spaces.learning.services.course_factory.learnhouse_gateway import build_learnhouse_http_gateway
    from spaces.learning.services.lernsequenz.cli import published_draft
    from spaces.learning.services.lernsequenz.einbettung import build_sequence_embedder

    embedder = build_sequence_embedder(build_learnhouse_http_gateway())
    if embedder is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Embedding is not configured")
    try:
        draft = published_draft(course_id)
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "No published course") from error
    result = await embedder.embed(course_id=course_id, draft=draft, correlation_id=str(context.correlation_id))
    return JSONResponse(result)


def _public_link(sequence_id: str) -> str:
    base = os.environ.get("LEARNING_PUBLIC_API_URL", "http://127.0.0.1:8090").rstrip("/")
    return f"{base}/lernsequenz/{sequence_id}?k={sequence_token(sequence_id)}"


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
