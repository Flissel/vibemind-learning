"""Notes over the service API: LearnHouse asks on behalf of the signed-in person.

Both headers are required — the shared service key (compared like LearnHouse compares
it) and the learner id the LearnHouse backend derived from its own session. The
browser never talks to these routes directly, so no reply carries the learner id.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request, status
from fastapi.responses import JSONResponse

from spaces.learning.deployment.lernende_auth import lernende_person
from spaces.learning.deployment.vibemind_auth import ApiError
from spaces.learning.services.notizen.store import OWN_TEXT_MAX, NoteStore

router = APIRouter()


@lru_cache(maxsize=1)
def _store() -> NoteStore:
    from spaces.learning.services.db.session import build_session_factory, create_learning_engine

    url = os.environ.get("LEARNING_DATABASE_URL", "").strip()
    if not url:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Learning database is not configured")
    return NoteStore(build_session_factory(create_learning_engine(url)))


def _actor(request: Request) -> str:
    return lernende_person(request)


@router.get("/api/v1/notizen")
def list_notes(request: Request) -> JSONResponse:
    actor_id = _actor(request)
    return JSONResponse({"notes": _store().list_for(actor_id)})


@router.patch("/api/v1/notizen/{note_id}")
def set_own_text(request: Request, note_id: str, payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    actor_id = _actor(request)
    own_text = payload.get("own_text")
    if not isinstance(own_text, str) or len(own_text) > OWN_TEXT_MAX:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", f"own_text must be text of at most {OWN_TEXT_MAX} characters")
    try:
        return JSONResponse(_store().set_own_text(note_id, actor_id, own_text))
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found", "Note not found") from error
