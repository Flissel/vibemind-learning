"""Signed link to the drawing page for exactly one canvas task of one person.

learning-mcp issues it when a person opens a canvas task (``learning_canvas_open``);
learning-api serves ``/zeichnen/<token>`` and acts as that person only. The token holds
the practice session, the task, the canvas reference and the person, signed with a key
derived from the Learning service key, and expires.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from uuid import UUID

PATH = "/zeichnen/"
TTL_SECONDS = 12 * 3600


@dataclass(frozen=True)
class DrawingGrant:
    session_id: str
    activity_id: str
    session_ref: str
    actor_id: str


def _key(service_key: str | None) -> bytes:
    key = service_key if service_key is not None else os.environ.get("LEARNING_VIBEMIND_SERVICE_KEY", "")
    if len(key) < 32:
        raise RuntimeError("Learning service key is not configured")
    return hashlib.sha256(b"zeichnen:" + key.encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def drawing_token(
    grant: DrawingGrant, *, service_key: str | None = None, now: float | None = None
) -> str:
    body = {
        "s": str(UUID(grant.session_id)),
        "a": str(UUID(grant.activity_id)),
        "r": grant.session_ref,
        "u": grant.actor_id,
        "e": int((now if now is not None else time.time()) + TTL_SECONDS),
    }
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(_key(service_key), payload.encode(), hashlib.sha256).hexdigest()[:48]
    return f"{payload}.{signature}"


def read_drawing_token(
    token: str, *, service_key: str | None = None, now: float | None = None
) -> DrawingGrant:
    """The grant of a valid token; LookupError for a forged, broken or expired one."""
    payload, _, signature = token.partition(".")
    expected = hmac.new(_key(service_key), payload.encode(), hashlib.sha256).hexdigest()[:48]
    if not payload or not hmac.compare_digest(expected, signature):
        raise LookupError("drawing link is not valid")
    try:
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        grant = DrawingGrant(
            session_id=str(UUID(body["s"])),
            activity_id=str(UUID(body["a"])),
            session_ref=str(body["r"]),
            actor_id=str(body["u"]),
        )
        expires = int(body["e"])
    except (KeyError, TypeError, ValueError) as error:
        raise LookupError("drawing link is not valid") from error
    if expires < (now if now is not None else time.time()):
        raise LookupError("drawing link has expired")
    return grant


def drawing_url(grant: DrawingGrant, *, base_url: str | None = None) -> str:
    base = (base_url if base_url is not None else os.environ.get("LEARNING_PUBLIC_API_URL", "")).rstrip("/")
    if not base:
        raise RuntimeError("Learning public API URL is not configured")
    return base + PATH + drawing_token(grant)
