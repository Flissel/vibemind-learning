from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from spaces.learning.bridge.dispatcher import InMemoryReceiptStore
from spaces.learning.contracts.events import LearningEventType, LearningToolName
from spaces.learning.contracts.mcp_models import ActorV1, EventEnvelopeV1, ToolRequestV1
from spaces.learning.mcp import server


class _LearnHouse:
    def execute(self, request):
        raise AssertionError("LearnHouse must not be called")

    def readback(self, request, outcome):
        return None


def _generate() -> ToolRequestV1:
    return ToolRequestV1(
        tool=LearningToolName.COURSE_GENERATE,
        event=EventEnvelopeV1(
            event_type=LearningEventType.COURSE_GENERATE,
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            actor=ActorV1(actor_id="local-owner", actor_type="local_user"),
            course_id=uuid4(),
            idempotency_key="role-generate",
            expected_revision=1,
            payload={"audience": "a", "target_outcome": "b", "source_ids": [str(uuid4())]},
        ),
    )


def test_misconfigured_course_factory_fails_closed_without_crashing_the_role(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LEARNING_SERVICE_ROLE", "api")
    monkeypatch.setenv("LEARNING_DATABASE_URL", f"sqlite:///{tmp_path / 'role.db'}")
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("LEARNHOUSE_LEARNING_SERVICE_KEY", raising=False)

    def broken(**kwargs):
        raise ValueError("LearnHouse service key is not configured securely")

    monkeypatch.setattr(server, "build_course_factory_gateways", broken)
    dispatcher = server.build_default_dispatcher(
        learnhouse=_LearnHouse(), receipts=InMemoryReceiptStore()
    )
    result = dispatcher.dispatch(_generate())

    assert result.state == "unavailable"
    assert result.error is not None
    assert result.error.code == "learning_backend_unavailable"
