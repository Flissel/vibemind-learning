from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from spaces.learning.contracts.course_factory_api import (
    ApiErrorV1,
    CourseGenerationRequestV1,
    HttpsImportSourceV1,
    LocalImportSourceV1,
    MaterialImportRequestV1,
    PublishRequestV1,
    ReviewRequestV1,
)


def _generation(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "course_id": str(uuid4()),
        "expected_course_revision": 3,
        "title": "Safety",
        "audience": "Engineers",
        "level": "advanced",
        "locale": "de-DE",
        "learning_objectives": ["Assess risk"],
        "source_ids": [str(uuid4())],
    }
    value.update(overrides)
    return value


def test_generation_request_accepts_bounded_grounded_input() -> None:
    request = CourseGenerationRequestV1.model_validate(_generation())
    assert request.level == "advanced"
    assert len(request.source_ids) == 1


def test_generation_request_rejects_empty_sources_and_provider_fields() -> None:
    with pytest.raises(ValidationError):
        CourseGenerationRequestV1.model_validate(_generation(source_ids=[]))
    with pytest.raises(ValidationError):
        CourseGenerationRequestV1.model_validate(_generation(provider="openai"))
    with pytest.raises(ValidationError):
        CourseGenerationRequestV1.model_validate(_generation(api_key="sk-test"))


def test_generation_request_rejects_duplicate_sources_and_bad_locale() -> None:
    source = str(uuid4())
    with pytest.raises(ValidationError):
        CourseGenerationRequestV1.model_validate(_generation(source_ids=[source, source]))
    with pytest.raises(ValidationError):
        CourseGenerationRequestV1.model_validate(_generation(locale="german"))
    with pytest.raises(ValidationError):
        CourseGenerationRequestV1.model_validate(_generation(learning_objectives=[]))


def test_generation_request_composes_one_bounded_target_outcome() -> None:
    request = CourseGenerationRequestV1.model_validate(
        _generation(learning_objectives=["Assess risk", "Plan mitigation"])
    )
    outcome = request.target_outcome()
    assert "Safety" in outcome
    assert "Assess risk" in outcome and "Plan mitigation" in outcome
    assert len(outcome) <= 2_000


def test_publish_requires_current_revision_and_confirmation() -> None:
    value = PublishRequestV1(expected_revision=7, confirmed=True, approval_ref="approval-7")
    assert value.expected_revision == 7
    with pytest.raises(ValidationError):
        PublishRequestV1.model_validate(
            {"expected_revision": 7, "confirmed": False, "approval_ref": "approval-7"}
        )
    with pytest.raises(ValidationError):
        PublishRequestV1.model_validate({"confirmed": True, "approval_ref": "approval-7"})


def test_review_accepts_only_explicit_decisions() -> None:
    assert ReviewRequestV1(expected_revision=2, decision="approve").decision == "approve"
    with pytest.raises(ValidationError):
        ReviewRequestV1.model_validate({"expected_revision": 2, "decision": "maybe"})


@pytest.mark.parametrize(
    "relative_path",
    ["/etc/passwd", "C:/Users/secret.md", "../outside.md", "a/../../b.md", "a\\b.md", ""],
)
def test_local_import_rejects_unsafe_paths(relative_path: str) -> None:
    with pytest.raises(ValidationError):
        LocalImportSourceV1(kind="local_import", relative_path=relative_path)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a.pdf",
        "ftp://example.com/a.pdf",
        "https://user:pass@example.com/a.pdf",
        "https://example.com/a.pdf#fragment",
        "https:///missing-host",
    ],
)
def test_https_import_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        HttpsImportSourceV1(kind="https", url=url)


def test_material_import_discriminates_source_kind() -> None:
    request = MaterialImportRequestV1.model_validate(
        {
            "course_id": str(uuid4()),
            "expected_course_revision": 1,
            "title": "Manual",
            "source": {"kind": "local_import", "relative_path": "manuals/manual.md"},
        }
    )
    assert isinstance(request.source, LocalImportSourceV1)
    remote = MaterialImportRequestV1.model_validate(
        {
            "course_id": str(uuid4()),
            "expected_course_revision": 1,
            "title": "Manual",
            "source": {"kind": "https", "url": "https://example.com/manual.md"},
        }
    )
    assert isinstance(remote.source, HttpsImportSourceV1)


def test_api_error_is_bounded() -> None:
    error = ApiErrorV1(code="not_found", message="x" * 500, retryable=False)
    assert error.correlation_id is None
    with pytest.raises(ValidationError):
        ApiErrorV1(code="not_found", message="x" * 501, retryable=False)


def test_generation_request_carries_a_persons_review_notes() -> None:
    # 27.10.: a rejected draft is rebuilt with the person's notes (fewer chapters, one address).
    request = CourseGenerationRequestV1.model_validate(_generation(reviewer_notes=["Höchstens sechs Kapitel."]))
    assert request.reviewer_notes == ("Höchstens sechs Kapitel.",)
    assert CourseGenerationRequestV1.model_validate(_generation()).reviewer_notes == ()
    with pytest.raises(ValidationError):
        CourseGenerationRequestV1.model_validate(_generation(reviewer_notes=["x"] * 11))
    with pytest.raises(ValidationError):
        CourseGenerationRequestV1.model_validate(_generation(reviewer_notes=["x" * 501]))
