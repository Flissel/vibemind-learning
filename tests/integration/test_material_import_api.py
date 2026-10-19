from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from spaces.learning.services.db.models import LearningArtifact
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


def _payload(harness: ApiHarness, relative_path: str = "manual.md") -> dict[str, object]:
    return {
        "course_id": harness.course_id,
        "expected_course_revision": 4,
        "title": "Manual",
        "source": {"kind": "local_import", "relative_path": relative_path},
    }


def test_material_import_stages_then_dispatches(client, harness) -> None:
    (harness.import_root / "manual.md").write_text(
        "# Safety\n\nAuthorized AI execution needs an approval reference.\n",
        encoding="utf-8",
    )
    response = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.author", "material-1"),
        json=_payload(harness),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["projection_state"] == "pending"
    assert body["evidence"]["evidence_type"] == "artifact_readback"
    assert str(harness.import_root) not in response.text
    with harness.session_factory() as session:
        artifact = session.get(LearningArtifact, body["artifact_id"])
    assert artifact is not None and artifact.aggregate_type == "source_upload"

    state = client.get(
        f"/api/v1/materials/{body['source_id']}", headers=headers("learning.author")
    )
    assert state.status_code == 200
    assert state.json()["revision"] == body["revision"]
    assert state.json()["projection_state"] == "pending"


def test_material_import_replay_returns_the_original_receipt(client, harness) -> None:
    (harness.import_root / "manual.md").write_text(
        "# Safety\n\nReplays must not create a second source.\n", encoding="utf-8"
    )
    first = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.author", "material-replay"),
        json=_payload(harness),
    )
    second = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.author", "material-replay"),
        json=_payload(harness),
    )
    changed = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.author", "material-replay"),
        json={**_payload(harness), "title": "Other"},
    )

    assert second.status_code == 202
    assert second.json()["source_id"] == first.json()["source_id"]
    assert changed.status_code == 409
    assert harness.learnhouse.calls.count(harness.learnhouse.calls[0]) == 1


def test_imported_material_grounds_a_generation_job(client, harness) -> None:
    (harness.import_root / "manual.md").write_text(
        "# Safety\n\nEvery AI run records its approval reference.\n", encoding="utf-8"
    )
    imported = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.author", "material-2"),
        json=_payload(harness),
    ).json()
    job = client.post(
        "/api/v1/course-factory/jobs",
        headers=headers("learning.author", "job-from-material"),
        json=harness.generation_payload(imported["source_id"]),
    )
    assert job.status_code == 202
    assert job.json()["state"] == "queued"


def test_material_import_rejects_unsafe_or_missing_files(client, harness) -> None:
    traversal = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.author", "m-trav"),
        json=_payload(harness, "../outside.md"),
    )
    missing = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.author", "m-miss"),
        json=_payload(harness, "missing.md"),
    )

    assert traversal.status_code == 400
    assert missing.status_code == 422
    assert missing.json()["code"] == "material_rejected"
    assert str(harness.import_root) not in missing.text


def test_material_import_requires_author_and_idempotency(client, harness) -> None:
    (harness.import_root / "manual.md").write_text("# Safety", encoding="utf-8")
    reviewer = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.reviewer", "m-r"),
        json=_payload(harness),
    )
    no_key = client.post(
        "/api/v1/materials/import", headers=headers("learning.author"), json=_payload(harness)
    )

    assert reviewer.status_code == 403
    assert no_key.status_code == 400


def test_unknown_material_is_not_found(client, harness) -> None:
    response = client.get(f"/api/v1/materials/{uuid4()}", headers=headers("learning.author"))
    assert response.status_code == 404


def test_material_import_gives_learnhouse_a_source_url(client, harness) -> None:
    (harness.import_root / "manual.md").write_text(
        "# Safety\n\nCheck the tool.\n", encoding="utf-8"
    )
    response = client.post(
        "/api/v1/materials/import",
        headers=headers("learning.author", "material-url-1"),
        json=_payload(harness),
    )

    assert response.status_code == 202
    (operation,) = harness.learnhouse.source_operations
    assert operation.payload["source_url"] == (
        f"learning-artifact://{response.json()['artifact_id']}"
    )
