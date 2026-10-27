from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.tests.integration.course_factory_api_support import (
    ApiHarness,
    clear_overrides,
    headers,
)


@pytest.fixture()
def harness(tmp_path: Path):
    value = ApiHarness(tmp_path)
    yield value
    clear_overrides()


@pytest.fixture()
def client(harness: ApiHarness, monkeypatch):
    return harness.client(monkeypatch)


def _create(client, harness: ApiHarness, key: str = "job-1", **overrides):
    return client.post(
        "/api/v1/course-factory/jobs",
        headers=headers("learning.author", key),
        json=harness.generation_payload(**overrides),
    )


def test_create_job_returns_202_and_persisted_revision(client, harness) -> None:
    response = _create(client, harness)

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "queued"
    assert body["outcome"] == "completed"
    assert body["course_id"] == harness.course_id
    job = harness.repository.get(body["job_id"])
    assert body["revision"] == job.revision
    assert body["evidence"]["evidence_type"] == "job_readback"


def test_replay_returns_the_same_job_without_a_second_effect(client, harness) -> None:
    first = _create(client, harness, key="same")
    second = _create(client, harness, key="same")

    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]


def test_idempotency_conflict_maps_to_409(client, harness) -> None:
    _create(client, harness, key="same")
    changed = _create(client, harness, key="same", title="Different")

    assert changed.status_code == 409
    assert changed.json()["code"] == "idempotency_conflict"


def test_mutations_require_an_idempotency_key(client, harness) -> None:
    response = client.post(
        "/api/v1/course-factory/jobs",
        headers=headers("learning.author"),
        json=harness.generation_payload(),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_authentication_and_authority_fail_closed(client, harness) -> None:
    payload = harness.generation_payload()
    url = "/api/v1/course-factory/jobs"
    bad_key = client.post(url, headers=headers("learning.author", "a", key="x" * 32), json=payload)
    wrong_role = client.post(url, headers=headers("learning.reviewer", "b"), json=payload)
    foreign = client.post(url, headers=headers("learning.author", "c", space="video"), json=payload)

    assert bad_key.status_code == 401 and bad_key.json()["code"] == "unauthenticated"
    assert wrong_role.status_code == 403 and wrong_role.json()["code"] == "forbidden"
    assert foreign.status_code == 403
    assert harness.receipts.get("a") is None and harness.receipts.get("b") is None


def test_unready_source_maps_to_422(client, harness) -> None:
    failed = harness.add_source(status="failed")
    response = _create(client, harness, source_ids=[failed])

    assert response.status_code == 422
    assert response.json()["code"] == "source_not_ready"


def test_validation_errors_are_redacted(client, harness) -> None:
    response = client.post(
        "/api/v1/course-factory/jobs",
        headers=headers("learning.author", "redact"),
        json=harness.generation_payload(provider="openai", audience="sk-" + "a" * 30),
    )
    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"code", "message", "retryable", "correlation_id"}
    assert "sk-" not in response.text and "openai" not in response.text


def test_job_status_readback_and_not_found(client, harness) -> None:
    created = _create(client, harness).json()
    status = client.get(
        f"/api/v1/course-factory/jobs/{created['job_id']}",
        headers=headers("learning.reviewer"),
    )
    missing = client.get(
        f"/api/v1/course-factory/jobs/{uuid4()}", headers=headers("learning.reviewer")
    )

    assert status.status_code == 200
    assert status.json()["job_id"] == created["job_id"]
    assert status.json()["revision"] == created["revision"]
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_review_requires_reviewer_and_current_revision(client, harness) -> None:
    created = _create(client, harness).json()
    url = f"/api/v1/course-factory/jobs/{created['job_id']}/review"
    as_author = client.post(
        url, headers=headers("learning.author", "r0"),
        json={"expected_revision": created["revision"], "decision": "approve"},
    )
    stale = client.post(
        url, headers=headers("learning.reviewer", "r1"),
        json={"expected_revision": created["revision"] + 3, "decision": "approve"},
    )
    approved = client.post(
        url, headers=headers("learning.reviewer", "r2"),
        json={"expected_revision": created["revision"], "decision": "approve"},
    )

    assert as_author.status_code == 403
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    assert approved.status_code == 200
    assert approved.json()["outcome"] == "approval_required"
    assert approved.json()["publish_confirmation_required"] is True


def test_review_cannot_reject_a_job_that_is_not_review_ready(client, harness) -> None:
    created = _create(client, harness).json()
    response = client.post(
        f"/api/v1/course-factory/jobs/{created['job_id']}/review",
        headers=headers("learning.reviewer", "reject-1"),
        json={"expected_revision": created["revision"], "decision": "reject"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_transition"
    assert harness.repository.get(created["job_id"]).state is FactoryState.QUEUED


def test_publish_requires_publisher_confirmation_and_review_ready(client, harness) -> None:
    created = _create(client, harness).json()
    url = f"/api/v1/course-factory/jobs/{created['job_id']}/publish"
    body = {"expected_revision": created["revision"], "confirmed": True, "approval_ref": "ok-1"}
    unconfirmed = client.post(
        url, headers=headers("learning.publisher", "p0"), json={**body, "confirmed": False}
    )
    as_reviewer = client.post(url, headers=headers("learning.reviewer", "p1"), json=body)
    stale = client.post(
        url, headers=headers("learning.publisher", "p2"),
        json={**body, "expected_revision": created["revision"] + 1},
    )
    unreviewed = client.post(url, headers=headers("learning.publisher", "p3"), json=body)

    assert unconfirmed.status_code == 400
    assert as_reviewer.status_code == 403
    assert stale.status_code == 409
    assert unreviewed.status_code == 422 and unreviewed.json()["code"] == "not_review_ready"
    assert harness.publisher.calls == 0


def test_course_projection_reads_through_learnhouse(client, harness) -> None:
    found = client.get(f"/api/v1/courses/{harness.course_id}", headers=headers("learning.author"))
    missing = client.get(f"/api/v1/courses/{uuid4()}", headers=headers("learning.author"))

    assert found.status_code == 200
    assert found.json()["revision"] == 4
    assert found.json()["course"]["title"] == "Grounded"
    assert missing.status_code == 404


def test_job_response_carries_the_draft_outline() -> None:
    import json
    from uuid import uuid4 as _uuid4

    from spaces.learning.contracts.outcomes import (
        AggregateRefV1,
        EvidenceRefV1,
        ToolResultV1,
    )
    from spaces.learning.deployment.course_factory_routes import _job_response
    from spaces.learning.deployment.vibemind_auth import VibeMindRequestContext

    job_id = str(_uuid4())
    summary = {
        "title": "Akku-Werkzeug sicher nutzen",
        "chapters": [
            {"title": "Vor der Arbeit", "lessons": ["Sichtpruefung"], "activity_count": 3}
        ],
    }
    result = ToolResultV1(
        invocation_id=_uuid4(),
        correlation_id=_uuid4(),
        state="completed",
        aggregate=AggregateRefV1(aggregate_type="factory_job", aggregate_id=job_id, revision=8),
        evidence=EvidenceRefV1(
            owner="course-factory",
            evidence_id=f"factory:{job_id}:8",
            evidence_type="job_readback",
        ),
        result={
            "course_id": str(_uuid4()),
            "state": "review_ready",
            "attempt_number": 1,
            "draft_summary": summary,
        },
    )
    context = VibeMindRequestContext(
        actor_id="local-owner",
        space_id="learning",
        roles=frozenset({"learning.author"}),
        correlation_id=_uuid4(),
    )

    body = json.loads(_job_response(result, context, 200).body)

    assert body["draft_summary"] == summary


def test_create_job_stores_a_persons_review_notes(client, harness) -> None:
    response = _create(client, harness, key="notes", reviewer_notes=["Höchstens sechs Kapitel."])

    assert response.status_code == 202
    stored = harness.repository.get_current_attempt(response.json()["job_id"]).generation_request
    assert stored.person_notes == ("Höchstens sechs Kapitel.",)
