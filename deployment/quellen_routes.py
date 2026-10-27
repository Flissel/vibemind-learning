"""Changed source pages for the claw: which courses should be looked at again."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from spaces.learning.deployment.vibemind_auth import ROLE_AUTHOR, ApiError, VibeMindRequestContext, vibemind_context
from spaces.learning.services.quellen.store import WatchStore

router = APIRouter()
Context = Annotated[VibeMindRequestContext, Depends(vibemind_context)]


@lru_cache(maxsize=1)
def _store() -> WatchStore:
    from spaces.learning.services.db.session import build_session_factory, create_learning_engine

    url = os.environ.get("LEARNING_DATABASE_URL", "").strip()
    if not url:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Learning database is not configured")
    return WatchStore(build_session_factory(create_learning_engine(url)))


@router.get("/api/v1/quellen/frische")
def changed_sources(context: Context) -> JSONResponse:
    """Source pages whose visible text changed since the course was built from them."""
    context.require(ROLE_AUTHOR)
    return JSONResponse({"changed": _store().changed()})
