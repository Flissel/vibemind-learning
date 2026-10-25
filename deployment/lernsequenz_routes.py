"""The sequence player: LearnHouse pages embed ``/lernsequenz/<id>?k=<token>``.

The page and its data carry no solutions (``public_view``); answers are graded here.
Each link holds an HMAC token for exactly one sequence, derived from the Learning service
key, so a sequence cannot be guessed or enumerated. v1 counts answers for the local
learner; mapping the LearnHouse person comes with the player integration (stage 0b).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from spaces.learning.contracts.lernsequenz import DrawnExplanation, public_view
from spaces.learning.deployment.vibemind_auth import ApiError
from spaces.learning.services.lernsequenz.bewertung import grade
from spaces.learning.services.lernsequenz.store import SequenceStore

_PLAYER = Path(__file__).with_name("lernsequenz_player.html")
_MEDIA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(mp4|webm|mp3|png|jpg|webp)$")
ANCESTORS_ENV = "LEARNING_EMBED_ANCESTORS"

router = APIRouter()
Token = Annotated[str, Query(alias="k", min_length=16, max_length=128)]


def sequence_token(sequence_id: str, service_key: str | None = None) -> str:
    key = service_key if service_key is not None else os.environ.get("LEARNING_VIBEMIND_SERVICE_KEY", "")
    if len(key) < 32:
        raise RuntimeError("Learning service key is not configured")
    derived = hashlib.sha256(b"lernsequenz:" + key.encode()).digest()
    return hmac.new(derived, sequence_id.encode(), hashlib.sha256).hexdigest()[:40]


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
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Checkpoint not found") from error
    except ValueError as error:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", str(error)) from error
    return JSONResponse(result, headers=_headers())


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
