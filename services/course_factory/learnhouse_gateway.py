from __future__ import annotations

import ipaddress
import json
import os
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from pydantic import ValidationError

from spaces.learning.services.course_factory.publisher import (
    DraftDeliveryReceipt,
    LearnHouseCourseReadback,
)
from spaces.learning.services.course_factory.schemas import CourseDraft, learnhouse_payload


_SERVICE_KEY_HEADER = "X-LearnHouse-Learning-Service-Key"
_CORRELATION_HEADER = "X-VibeMind-Correlation-ID"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ALLOWED_SERVICE_HOSTS = frozenset({"localhost", "learnhouse-api"})


class LearnHouseGatewayError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _fehler_code(body: bytes) -> str | None:
    """The error code of a rejected response, if its (small) JSON body carries one."""
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    detail = value.get("detail")
    kandidat = detail.get("code") if isinstance(detail, dict) else detail
    if not isinstance(kandidat, str):
        kandidat = value.get("code")
    return kandidat if isinstance(kandidat, str) else None


class LearnHouseHttpGateway:
    def __init__(
        self,
        base_url: str,
        service_key: str,
        *,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        _validate_internal_url(base_url)
        if len(service_key) < 32:
            raise ValueError("LearnHouse service key is not configured securely")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("LearnHouse gateway timeout is invalid")
        # Without an injected client, each request opens its own: callers run us
        # via asyncio.run() per request, and pooled connections die with their loop.
        self._client = client
        self._base_url = base_url.rstrip("/") + "/"
        self._timeout_seconds = timeout_seconds
        self._service_key = service_key

    async def stage_draft(
        self,
        *,
        course_id: str,
        expected_revision: int,
        draft: CourseDraft,
        draft_hash: str,
        correlation_id: str,
    ) -> DraftDeliveryReceipt:
        value = await self._request_json(
            "PUT",
            f"courses/{course_id}/factory-draft",
            correlation_id=correlation_id,
            payload={
                "expected_revision": expected_revision,
                "draft_hash": draft_hash,
                "draft": learnhouse_payload(draft),
            },
        )
        try:
            readback = value["draft"]
            return DraftDeliveryReceipt(
                course_id=course_id,
                factory_job_id=readback["factory_job_id"],
                attempt_number=readback["attempt_number"],
                draft_hash=readback["draft_hash"],
                learnhouse_revision=readback["revision"],
                evidence_ref=(
                    f"learnhouse://courses/{course_id}/factory-draft/"
                    f"revision/{readback['revision']}"
                ),
            )
        except (KeyError, TypeError, ValidationError) as error:
            raise LearnHouseGatewayError("LearnHouse draft response is invalid") from error

    async def approve(
        self, *, course_id: str, expected_revision: int, correlation_id: str
    ) -> LearnHouseCourseReadback:
        return await self._course_mutation(
            course_id=course_id,
            action="review",
            correlation_id=correlation_id,
            payload={"review": "approved", "expected_revision": expected_revision},
        )

    async def publish(
        self, *, course_id: str, expected_revision: int, correlation_id: str
    ) -> LearnHouseCourseReadback:
        return await self._course_mutation(
            course_id=course_id,
            action="publish",
            correlation_id=correlation_id,
            payload={"expected_revision": expected_revision},
        )

    async def read_course(
        self, *, course_id: str, correlation_id: str
    ) -> LearnHouseCourseReadback:
        value = await self._request_json(
            "GET",
            f"courses/{course_id}",
            correlation_id=correlation_id,
        )
        return _parse_course(value)

    async def embed_sequences(
        self, *, course_id: str, lesson_id: str, embeds: list[dict[str, object]], correlation_id: str
    ) -> dict[str, object]:
        """Put Learning's sequence players (presentation, video) into a factory lesson."""
        return await self._request_json(
            "PUT",
            f"courses/{course_id}/lernsequenz-einbettungen",
            correlation_id=correlation_id,
            payload={"lesson_id": lesson_id, "embeds": embeds},
        )

    async def werkstatt_stand_schreiben(
        self,
        course_id: str,
        *,
        expected_revision: int,
        freigabe_id: str,
        kapitel: list[dict[str, object]],
    ) -> dict[str, object]:
        """Write the released Werkstatt state (chapters, lessons, tasks) into the course."""
        return await self._request_json(
            "PUT",
            f"courses/{course_id}/werkstatt-stand",
            correlation_id=str(uuid4()),
            payload={
                "expected_revision": expected_revision,
                "freigabe_id": freigabe_id,
                "kapitel": kapitel,
            },
        )

    async def _course_mutation(
        self,
        *,
        course_id: str,
        action: str,
        correlation_id: str,
        payload: dict[str, object],
    ) -> LearnHouseCourseReadback:
        value = await self._request_json(
            "POST",
            f"courses/{course_id}/{action}",
            correlation_id=correlation_id,
            payload=payload,
        )
        return _parse_course(value)

    async def _read(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        correlation_id: str,
        payload: dict[str, object] | None,
    ) -> bytearray:
        async with client.stream(
            method,
            path,
            headers={
                _SERVICE_KEY_HEADER: self._service_key,
                _CORRELATION_HEADER: correlation_id,
            },
            json=payload,
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 64 * 1024:
                        break
                raise LearnHouseGatewayError(
                    f"LearnHouse request was rejected with {response.status_code}",
                    status=response.status_code,
                    code=_fehler_code(bytes(body)),
                )
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise LearnHouseGatewayError("LearnHouse response exceeds the size limit")
        return content

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        correlation_id: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            if self._client is not None:
                content = await self._read(self._client, method, path, correlation_id, payload)
            else:
                async with httpx.AsyncClient(
                    base_url=self._base_url, timeout=self._timeout_seconds
                ) as client:
                    content = await self._read(client, method, path, correlation_id, payload)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise LearnHouseGatewayError("LearnHouse is unavailable") from error
        try:
            value = json.loads(content)
        except (TypeError, ValueError) as error:
            raise LearnHouseGatewayError("LearnHouse response is not JSON") from error
        if not isinstance(value, dict):
            raise LearnHouseGatewayError("LearnHouse response must be an object")
        return value


def build_learnhouse_http_gateway() -> LearnHouseHttpGateway:
    return LearnHouseHttpGateway(
        os.environ.get("LEARNHOUSE_LEARNING_SERVICE_URL", "").strip(),
        os.environ.get("LEARNHOUSE_LEARNING_SERVICE_KEY", ""),
    )


def _parse_course(value: dict[str, object]) -> LearnHouseCourseReadback:
    try:
        course = value["course"]
        if not isinstance(course, dict):
            raise TypeError
        return LearnHouseCourseReadback(
            course_id=course["course_id"],
            revision=course["revision"],
            review_status=course["review_status"],
            published=course["published"],
        )
    except (KeyError, TypeError, ValidationError) as error:
        raise LearnHouseGatewayError("LearnHouse course response is invalid") from error


def _validate_internal_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("LearnHouse service URL must use HTTP or HTTPS")
    hostname = parsed.hostname.lower()
    is_loopback = False
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if hostname not in _ALLOWED_SERVICE_HOSTS and not is_loopback:
        raise ValueError("LearnHouse service URL must be internal or loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LearnHouse service URL contains forbidden components")
