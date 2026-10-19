"""VibeMind caller authority for the Learning HTTP adapter.

V1 accepts one loopback service key. It maps the caller onto a typed context
(actor, canonical Space, roles, correlation) and fails closed on every gap:
an insecure server key is 503, a bad credential 401, a foreign Space or a
missing/unknown role 403. Browser cookies are never service credentials.
"""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Header, HTTPException, status


SERVICE_KEY_ENV = "LEARNING_VIBEMIND_SERVICE_KEY"
SERVICE_KEY_MIN_LENGTH = 32

ROLE_AUTHOR = "learning.author"
ROLE_REVIEWER = "learning.reviewer"
ROLE_PUBLISHER = "learning.publisher"
KNOWN_ROLES = frozenset({ROLE_AUTHOR, ROLE_REVIEWER, ROLE_PUBLISHER})

_CANONICAL_SPACE = "learning"
_SAFE_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ApiError(HTTPException):
    """HTTP error carrying the stable public error code."""

    def __init__(
        self, status_code: int, code: str, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message[:500]
        self.retryable = retryable


@dataclass(frozen=True)
class VibeMindRequestContext:
    actor_id: str
    space_id: Literal["learning"]
    roles: frozenset[str]
    correlation_id: UUID

    def require(self, role: str) -> None:
        if role not in self.roles:
            raise ApiError(status.HTTP_403_FORBIDDEN, "forbidden", "Missing Learning role")


def require_vibemind_context(
    *,
    authorization: str,
    actor_id: str,
    space_id: str,
    roles: str,
    correlation_id: str,
) -> VibeMindRequestContext:
    configured = os.environ.get(SERVICE_KEY_ENV, "")
    if len(configured) < SERVICE_KEY_MIN_LENGTH:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "dependency_unavailable",
            "Learning API authority is not configured securely",
            retryable=True,
        )
    scheme, _, supplied = authorization.partition(" ")
    if scheme != "Bearer" or not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), configured.encode("utf-8")
    ):
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED, "unauthenticated", "Invalid VibeMind credentials"
        )
    if not _SAFE_ACTOR.match(actor_id):
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED, "unauthenticated", "Invalid VibeMind actor"
        )
    if space_id.strip().lower() != _CANONICAL_SPACE:
        raise ApiError(
            status.HTTP_403_FORBIDDEN, "forbidden", "Request is not bound to the Learning Space"
        )
    granted = frozenset(item.strip() for item in roles.split(",") if item.strip())
    if not granted or not granted <= KNOWN_ROLES:
        raise ApiError(status.HTTP_403_FORBIDDEN, "forbidden", "Unknown or missing Learning role")
    try:
        correlation = UUID(correlation_id)
    except ValueError as error:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST, "invalid_request", "Correlation ID must be a UUID"
        ) from error
    return VibeMindRequestContext(
        actor_id=actor_id,
        space_id=_CANONICAL_SPACE,
        roles=granted,
        correlation_id=correlation,
    )


def vibemind_context(
    authorization: Annotated[str, Header(max_length=1_024)] = "",
    actor_id: Annotated[str, Header(alias="X-VibeMind-Actor-ID", max_length=128)] = "",
    space_id: Annotated[str, Header(alias="X-VibeMind-Space-ID", max_length=64)] = "",
    roles: Annotated[str, Header(alias="X-VibeMind-Roles", max_length=256)] = "",
    correlation_id: Annotated[
        str, Header(alias="X-VibeMind-Correlation-ID", max_length=64)
    ] = "",
) -> VibeMindRequestContext:
    """FastAPI dependency projecting the VibeMind headers onto the context."""
    return require_vibemind_context(
        authorization=authorization,
        actor_id=actor_id,
        space_id=space_id,
        roles=roles,
        correlation_id=correlation_id,
    )
