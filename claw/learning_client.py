"""HTTP/MCP client the claw sidecar uses to reach the Learning service.

Only the Learning API (``/api/v1``) and the Learning MCP endpoint are reachable
from here; the client never talks to LearnHouse or Penecho directly. The
service key is sent as a bearer token and is scrubbed from every message the
client raises or logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

import httpx


logger = logging.getLogger(__name__)

ACTOR_ID = "learning-claw"
SPACE_ID = "learning"
_TIMEOUT_SECONDS = 30.0
_MESSAGE_LIMIT = 500


class LearningUnavailable(Exception):
    """The Learning service could not be reached or answered with a 5xx."""


class LearningRejected(Exception):
    """The Learning service answered with a typed refusal."""

    def __init__(
        self, code: str, message: str, status: int, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        # Extra fields of the refusal body worth showing the model (e.g. ``neueste``).
        self.details = details or {}


def idempotency_key(tool: str, args: dict[str, Any]) -> str:
    """Stable key: identical tool and arguments always yield the same key."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256((tool + canonical).encode("utf-8")).hexdigest()
    return "claw-" + digest[:40]


class LearningClient:
    def __init__(
        self,
        api_url: str,
        mcp_url: str,
        service_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.mcp_url = mcp_url
        self._key = service_key
        self._http = httpx.Client(transport=transport, timeout=_TIMEOUT_SECONDS)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LearningClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        roles: str,
        idempotency_key_value: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key_value or idempotency_key(path, body)
        return self._request(
            "POST", self.api_url + path, roles, json_body=body,
            extra={"Idempotency-Key": key, **(extra or {})},
        )

    def get(
        self, path: str, *, roles: str, extra: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return self._request("GET", self.api_url + path, roles, extra=extra)

    def patch(
        self, path: str, body: dict[str, Any], *, roles: str, extra: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return self._request("PATCH", self.api_url + path, roles, json_body=body, extra=extra)

    def delete(
        self, path: str, *, roles: str, extra: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return self._request("DELETE", self.api_url + path, roles, extra=extra)

    def mcp_call(
        self, tool: str, event: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": event},
        }
        message = self._request(
            "POST", self.mcp_url, None, json_body=envelope, timeout=timeout
        )
        result = message.get("result")
        if not isinstance(result, dict):
            raise LearningUnavailable("Learning MCP lieferte keine Antwort")
        try:
            payload = json.loads(result["content"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise LearningUnavailable("Learning MCP-Antwort nicht lesbar") from error
        if not isinstance(payload, dict):
            raise LearningUnavailable("Learning MCP-Antwort nicht lesbar")
        if result.get("isError"):
            code, text = _mcp_error(payload)
            raise LearningRejected(code, self._scrub(text), 200)
        return payload

    def _request(
        self,
        method: str,
        url: str,
        roles: str | None,
        *,
        json_body: dict[str, Any] | None = None,
        extra: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "X-VibeMind-Actor-ID": ACTOR_ID,
            "X-VibeMind-Space-ID": SPACE_ID,
            "X-VibeMind-Correlation-ID": str(uuid4()),
        }
        if roles is not None:
            headers["X-VibeMind-Roles"] = roles
        headers.update(extra or {})
        try:
            response = self._http.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=timeout if timeout is not None else _TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            logger.warning("Learning request failed: %s", type(error).__name__)
            raise LearningUnavailable("Learning ist nicht erreichbar") from error
        if response.status_code >= 500:
            logger.warning("Learning answered %s", response.status_code)
            raise LearningUnavailable("Learning ist nicht erreichbar")
        data = _json_object(response)
        if response.status_code >= 400:
            code = data.get("code")
            message = data.get("message")
            raise LearningRejected(
                code if isinstance(code, str) else f"http_{response.status_code}",
                self._scrub(message if isinstance(message, str) else ""),
                response.status_code,
                _details(data),
            )
        return data

    def _scrub(self, text: str) -> str:
        scrubbed = text.replace(self._key, "[redacted]") if self._key else text
        return scrubbed[:_MESSAGE_LIMIT]


def _details(data: dict[str, Any]) -> dict[str, Any]:
    neueste = data.get("neueste")
    return {"neueste": neueste} if isinstance(neueste, dict) else {}


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _mcp_error(payload: dict[str, Any]) -> tuple[str, str]:
    error = payload.get("error")
    if isinstance(error, str):
        return error, error
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return (
            code if isinstance(code, str) else "mcp_error",
            message if isinstance(message, str) else "",
        )
    return "mcp_error", ""
