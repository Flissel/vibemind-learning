"""HTTP and MCP are two projections of one Course Factory application."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from spaces.learning.bridge.dispatcher import LearningDispatcher
from spaces.learning.contracts.events import LearningEventType, LearningToolName
from spaces.learning.contracts.mcp_models import ActorV1, ConfirmationV1, EventEnvelopeV1
from spaces.learning.deployment import runtime_api
from spaces.learning.deployment.course_factory_routes import (
    CourseFactoryRuntime,
    get_course_factory_runtime,
)
from spaces.learning.mcp import server
from spaces.learning.mcp.tools.generation import build_generation_gateways
from spaces.learning.services.course_factory.repository import CourseFactoryRepository
from spaces.learning.services.course_factory.application import CourseFactoryApplication
from spaces.learning.services.course_factory.artifact_store import (
    CourseFactoryArtifactStore,
)
from spaces.learning.services.course_factory.model_gateway import GatewaySchemaError
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.db.repository import SqlReceiptStore
from spaces.learning.tests.integration.course_factory_api_support import (
    ApiHarness,
    clear_overrides,
    headers,
)
from spaces.learning.tests.integration.test_generation_flow import (  # noqa: F401
    _GenerationGateway,
    _runner,
    generation_context,
)


@pytest.fixture()
def harness(tmp_path: Path):
    value = ApiHarness(tmp_path)
    yield value
    clear_overrides()


def _mcp(dispatcher: LearningDispatcher, tool: LearningToolName, event: EventEnvelopeV1) -> dict[str, Any]:
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": str(event.invocation_id),
            "method": "tools/call",
            "params": {"name": tool.value, "arguments": event.model_dump(mode="json")},
        },
        dispatcher=dispatcher,
    )
    assert response is not None
    return json.loads(response["result"]["content"][0]["text"])


def _event(event_type: LearningEventType, **values: Any) -> EventEnvelopeV1:
    return EventEnvelopeV1(
        event_type=event_type,
        invocation_id=uuid4(),
        correlation_id=values.pop("correlation_id", uuid4()),
        actor=ActorV1(actor_id="local-owner", actor_type="local_user"),
        **values,
    )


def _http_create(client, harness: ApiHarness, key: str = "http-job") -> dict[str, Any]:
    response = client.post(
        "/api/v1/course-factory/jobs",
        headers=headers("learning.author", key),
        json=harness.generation_payload(),
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_http_create_and_mcp_status_observe_same_job(harness, monkeypatch) -> None:
    client = harness.client(monkeypatch)
    created = _http_create(client, harness)

    observed = _mcp(
        harness.dispatcher,
        LearningToolName.GENERATION_STATUS,
        _event(LearningEventType.GENERATION_STATUS, payload={"job_id": created["job_id"]}),
    )

    assert observed["state"] == "completed"
    assert observed["aggregate"]["aggregate_id"] == created["job_id"]
    assert observed["aggregate"]["revision"] == created["revision"]
    assert observed["result"]["state"] == created["state"]
    assert observed["evidence"]["evidence_id"] == created["evidence"]["evidence_id"]


def test_mcp_create_and_http_status_observe_same_job(harness, monkeypatch) -> None:
    client = harness.client(monkeypatch)
    created = _mcp(
        harness.dispatcher,
        LearningToolName.COURSE_GENERATE,
        _event(
            LearningEventType.COURSE_GENERATE,
            course_id=UUID(harness.course_id),
            expected_revision=4,
            idempotency_key="mcp-job",
            payload={
                "audience": "Engineers",
                "target_outcome": "Assess AI risk",
                "source_ids": [harness.ready_source],
            },
        ),
    )
    job_id = created["result"]["job_id"]

    http = client.get(f"/api/v1/course-factory/jobs/{job_id}", headers=headers("learning.author"))

    assert http.status_code == 200
    assert http.json()["revision"] == created["aggregate"]["revision"]
    assert http.json()["state"] == created["result"]["state"]


def test_http_replay_survives_an_api_restart(harness, monkeypatch) -> None:
    client = harness.client(monkeypatch)
    created = _http_create(client, harness, key="restart-key")

    # A new process: fresh application, gateways and receipt store over the same DB.
    application = CourseFactoryApplication(
        session_factory=harness.session_factory,
        repository=CourseFactoryRepository(harness.session_factory),
        publisher=harness.publisher,
        artifact_store=CourseFactoryArtifactStore(
            harness.session_factory, artifact_root=harness.artifact_root
        ),
    )
    restarted = CourseFactoryRuntime(
        dispatcher=LearningDispatcher(
            gateways=build_generation_gateways(
                application=application, learnhouse_fallback=harness.learnhouse
            ),
            receipts=SqlReceiptStore(harness.session_factory),
        ),
        session_factory=harness.session_factory,
        stager=harness.stager,
    )
    runtime_api.app.dependency_overrides[get_course_factory_runtime] = lambda: restarted
    replayed = _http_create(client, harness, key="restart-key")

    assert replayed["job_id"] == created["job_id"]
    assert replayed["revision"] == created["revision"]


def test_http_and_mcp_agree_on_idempotency_conflicts(harness, monkeypatch) -> None:
    client = harness.client(monkeypatch)
    _http_create(client, harness, key="shared-key")

    conflict = _mcp(
        harness.dispatcher,
        LearningToolName.COURSE_GENERATE,
        _event(
            LearningEventType.COURSE_GENERATE,
            course_id=UUID(harness.course_id),
            expected_revision=4,
            idempotency_key="shared-key",
            payload={
                "audience": "Someone else",
                "target_outcome": "Different",
                "source_ids": [harness.ready_source],
            },
        ),
    )
    http = client.post(
        "/api/v1/course-factory/jobs",
        headers=headers("learning.author", "shared-key"),
        json=harness.generation_payload(title="Changed"),
    )

    assert conflict["state"] == "rejected"
    assert conflict["error"]["code"] == "idempotency_conflict"
    assert http.status_code == 409 and http.json()["code"] == "idempotency_conflict"


def test_publication_requires_confirmation_on_both_transports(harness, monkeypatch) -> None:
    client = harness.client(monkeypatch)
    created = _http_create(client, harness)
    missing = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "publish-without-confirmation",
            "method": "tools/call",
            "params": {
                "name": LearningToolName.COURSE_PUBLISH.value,
                "arguments": {
                    "event_type": LearningEventType.COURSE_PUBLISH.value,
                    "invocation_id": str(uuid4()),
                    "correlation_id": str(uuid4()),
                    "actor": {"actor_id": "local-owner", "actor_type": "local_user"},
                    "course_id": harness.course_id,
                    "idempotency_key": "publish-no-confirm",
                    "expected_revision": created["revision"],
                    "payload": {"job_id": created["job_id"]},
                },
            },
        },
        dispatcher=harness.dispatcher,
    )
    http = client.post(
        f"/api/v1/course-factory/jobs/{created['job_id']}/publish",
        headers=headers("learning.publisher", "publish-no-confirm-http"),
        json={"expected_revision": created["revision"], "approval_ref": "approval-1"},
    )

    assert json.loads(missing["result"]["content"][0]["text"]) == {"error": "invalid_arguments"}
    assert http.status_code == 400
    assert harness.publisher.calls == 0


class _SchemaInvalidGateway(_GenerationGateway):
    """OpenFang stand-in whose lesson author returns schema-invalid output."""

    async def generate(self, invocation, output_model):
        if invocation.role == "lesson_author":
            self.calls.append(invocation.role)
            raise GatewaySchemaError("OpenFang model response is schema-invalid")
        return await super().generate(invocation, output_model)


@pytest.mark.asyncio
async def test_schema_invalid_openfang_output_cannot_reach_review_ready(
    generation_context,  # noqa: F811  (fixture imported from test_generation_flow)
) -> None:
    factory, root, repository, job, draft = generation_context
    gateway = _SchemaInvalidGateway(draft)

    with pytest.raises(GatewaySchemaError):
        await _runner(factory, root, repository, gateway).run_job(job.id)

    current = repository.get(job.id)
    assert current.state is not FactoryState.REVIEW_READY
    assert "source_verifier" not in gateway.calls
    application = CourseFactoryApplication(
        session_factory=factory,
        repository=repository,
        publisher=None,  # type: ignore[arg-type]  # publication must be refused first
        artifact_store=CourseFactoryArtifactStore(factory, artifact_root=root),
    )
    dispatcher = LearningDispatcher(
        gateways=build_generation_gateways(application=application, learnhouse_fallback=None),  # type: ignore[arg-type]
        receipts=SqlReceiptStore(factory),
    )
    refused = _mcp(
        dispatcher,
        LearningToolName.COURSE_PUBLISH,
        _event(
            LearningEventType.COURSE_PUBLISH,
            course_id=UUID(current.course_id),
            expected_revision=current.revision,
            idempotency_key="publish-after-schema-failure",
            confirmation=ConfirmationV1(confirmed=True, approval_ref="approval-1"),
            payload={"job_id": job.id},
        ),
    )
    assert refused["state"] == "rejected"
    assert refused["error"]["code"] == "not_review_ready"
