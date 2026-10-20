"""Public HTTP contracts of the VibeMind Course Factory adapter.

The HTTP adapter is a second transport for the existing Learning MCP
operations, not a second authority. Every model is frozen and rejects unknown
fields, so provider selection, credentials, and host paths cannot be smuggled
into a request.
"""

from __future__ import annotations

import ipaddress
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, JsonValue, field_validator

from .mcp_models import ContractModel, SafeToken
from .outcomes import EvidenceRefV1


BoundedText = Annotated[str, Field(min_length=1, max_length=500)]
Locale = Annotated[str, Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")]

_TARGET_OUTCOME_LIMIT = 2_000
_URL_LIMIT = 2_048


def _strip_required(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("value must not be blank")
    return stripped


class CourseGenerationRequestV1(ContractModel):
    course_id: UUID
    expected_course_revision: Annotated[int, Field(ge=0)]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    audience: Annotated[str, Field(min_length=1, max_length=1_000)]
    level: Literal["beginner", "intermediate", "advanced", "expert"]
    locale: Locale
    learning_objectives: Annotated[
        tuple[BoundedText, ...], Field(min_length=1, max_length=32)
    ]
    source_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=256)]

    @field_validator("title", "audience")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _strip_required(value)

    @field_validator("learning_objectives")
    @classmethod
    def _objectives_not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip_required(item) for item in value)

    @field_validator("source_ids")
    @classmethod
    def _unique_sources(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(value) != len(set(value)):
            raise ValueError("source_ids must be unique")
        return value

    def target_outcome(self) -> str:
        """Compose the one bounded outcome text the Course Factory team consumes."""
        lines = [
            f"Course: {self.title}",
            f"Level: {self.level}",
            f"Locale: {self.locale}",
            "Learning objectives:",
            *(f"- {item}" for item in self.learning_objectives),
        ]
        composed = "\n".join(lines)
        if len(composed) > _TARGET_OUTCOME_LIMIT:
            raise ValueError("learning objectives exceed the generation input bound")
        return composed


class ReviewRequestV1(ContractModel):
    expected_revision: Annotated[int, Field(ge=0)]
    decision: Literal["approve", "reject"]
    reason: BoundedText | None = None


class PublishRequestV1(ContractModel):
    expected_revision: Annotated[int, Field(ge=0)]
    confirmed: Literal[True]
    approval_ref: SafeToken


class LocalImportSourceV1(ContractModel):
    kind: Literal["local_import"]
    relative_path: Annotated[str, Field(min_length=1, max_length=512)]

    @field_validator("relative_path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        if "\\" in value or ":" in value or "\x00" in value:
            raise ValueError("relative_path must be a POSIX path without drive or NUL")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("relative_path must stay beneath the import root")
        return path.as_posix()


class HttpsImportSourceV1(ContractModel):
    kind: Literal["https"]
    url: Annotated[str, Field(min_length=9, max_length=_URL_LIMIT)]

    @field_validator("url")
    @classmethod
    def _safe_https_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise ValueError("remote material must use https")
        if not parts.hostname:
            raise ValueError("remote material requires a host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("remote material URLs must not carry credentials")
        if parts.fragment:
            raise ValueError("remote material URLs must not carry fragments")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            return value
        if not address.is_global:
            raise ValueError("remote material must resolve to a public address")
        return value


MaterialSourceV1 = Annotated[
    LocalImportSourceV1 | HttpsImportSourceV1, Field(discriminator="kind")
]


class MaterialImportRequestV1(ContractModel):
    course_id: UUID
    expected_course_revision: Annotated[int, Field(ge=0)]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    source: MaterialSourceV1

    @field_validator("title")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _strip_required(value)


OutcomeState = Literal[
    "accepted", "rejected", "approval_required", "unavailable", "completed"
]


class DraftChapterSummaryV1(ContractModel):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    lessons: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        max_length=1_000
    )
    activity_count: Annotated[int, Field(ge=0, le=2_000)]


class DraftSummaryV1(ContractModel):
    """Outline of the verified draft; what the person approves for publication."""

    title: Annotated[str, Field(min_length=1, max_length=500)]
    chapters: tuple[DraftChapterSummaryV1, ...] = Field(max_length=500)


class FactoryJobResponseV1(ContractModel):
    correlation_id: UUID
    outcome: OutcomeState
    job_id: SafeToken
    course_id: SafeToken
    state: SafeToken
    attempt_number: Annotated[int, Field(ge=0)]
    revision: Annotated[int, Field(ge=0)]
    publish_confirmation_required: bool = False
    unsupported_claim_ids: tuple[SafeToken, ...] = ()
    draft_summary: DraftSummaryV1 | None = None
    evidence: EvidenceRefV1 | None = None


class MaterialImportResponseV1(ContractModel):
    correlation_id: UUID
    outcome: OutcomeState
    source_id: SafeToken
    revision: Annotated[int, Field(ge=0)]
    artifact_id: SafeToken
    projection_state: Literal["pending", "complete", "failed"]
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    locators: tuple[
        Annotated[str, Field(pattern=r"^source://[A-Za-z0-9/._:-]{1,500}$")], ...
    ] = ()
    evidence: EvidenceRefV1 | None = None


class MaterialStateResponseV1(ContractModel):
    source_id: SafeToken
    course_id: SafeToken
    revision: Annotated[int, Field(ge=0)]
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    projection_state: Literal["pending", "complete", "failed"]


class CourseProjectionResponseV1(ContractModel):
    correlation_id: UUID
    outcome: OutcomeState
    course_id: SafeToken
    revision: Annotated[int, Field(ge=0)]
    course: dict[str, JsonValue]
    evidence: EvidenceRefV1 | None = None


class ApiErrorV1(ContractModel):
    code: SafeToken
    message: Annotated[str, Field(min_length=1, max_length=500)]
    retryable: bool
    correlation_id: UUID | None = None
