from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from spaces.learning.services.adaptive_engine.session_service import (
    AuditReceipt,
    PublicTask,
    SessionProgress,
    SessionTurn,
)
from spaces.learning.mcp.tools.tutor import TutorService
from spaces.learning.services.course_factory.model_gateway import GatewayResult
from spaces.learning.services.evaluation.schemas import ChoiceOption


@dataclass
class SessionStub:
    mode: str = "training"

    def current(self, session_id: str, *, actor_id: str) -> SessionTurn:
        return SessionTurn(
            session_id=session_id,
            course_id=str(uuid4()),
            session_revision=4,
            state="active",
            mode=self.mode,
            task=PublicTask(
                id=str(uuid4()),
                type="single_choice",
                prompt="Welche Quelle ist autorisiert?",
                options=(ChoiceOption(id="a", label="Kursquelle"),),
                difficulty=0.4,
                representation="quiz",
                source_refs=("source://course/4",),
                presentation={},
            ),
            progress=SessionProgress(completed=1, total=4),
            audit=AuditReceipt(readback_verified=True),
        )


class GatewayStub:
    def __init__(self, *, unsupported: bool = False) -> None:
        self.invocation = None
        self.unsupported = unsupported

    async def generate(self, invocation, output_model):
        self.invocation = invocation
        return GatewayResult(
            output=output_model(
                schema_version="learning-tutor-v1",
                answer="Vergleiche die Quelle mit der Freigabeliste.",
                source_refs=[
                    "source://outside/1" if self.unsupported else "source://course/4"
                ],
                next_step="Begruende deine Auswahl.",
            ),
            evidence_ref="openfang://completion/tutor-1",
        )


@pytest.mark.asyncio
async def test_tutor_uses_authorized_openfang_role_and_current_task_sources() -> None:
    gateway = GatewayStub()
    service = TutorService(sessions=SessionStub(), gateway=gateway)
    result = await service.ask(
        session_id=str(uuid4()),
        actor_id="student-1",
        question="Wie gehe ich vor?",
        correlation_id=str(uuid4()),
    )
    assert result.answer.startswith("Vergleiche")
    assert result.source_refs == ("source://course/4",)
    assert result.evidence_ref == "openfang://completion/tutor-1"
    assert gateway.invocation.role == "learning_tutor"
    assert gateway.invocation.input_payload["sources"] == [
        {"key": "S1", "source": "source://course/4"}
    ]


@pytest.mark.asyncio
async def test_tutor_cites_short_source_keys_mapped_back_to_full_refs() -> None:
    # Live the long canonical refs were not echoed verbatim and every tutor
    # answer failed with "unsupported source references".
    class KeyStub(GatewayStub):
        async def generate(self, invocation, output_model):
            result = await super().generate(invocation, output_model)
            return GatewayResult(
                output=result.output.model_copy(update={"source_refs": ["S1"]}),
                evidence_ref=result.evidence_ref,
            )

    result = await TutorService(sessions=SessionStub(), gateway=KeyStub()).ask(
        session_id=str(uuid4()),
        actor_id="student-1",
        question="Wie gehe ich vor?",
        correlation_id=str(uuid4()),
    )
    assert result.source_refs == ("source://course/4",)


@pytest.mark.asyncio
async def test_tutor_rejects_unsupported_sources_and_exam_queries() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        await TutorService(sessions=SessionStub(), gateway=GatewayStub(unsupported=True)).ask(
            session_id=str(uuid4()),
            actor_id="student-1",
            question="Wie gehe ich vor?",
            correlation_id=str(uuid4()),
        )

    with pytest.raises(PermissionError, match="exam"):
        await TutorService(sessions=SessionStub(mode="exam"), gateway=GatewayStub()).ask(
            session_id=str(uuid4()),
            actor_id="student-1",
            question="Was ist die Loesung?",
            correlation_id=str(uuid4()),
        )


def test_tutor_gateway_says_when_no_task_is_open() -> None:
    # Live, a tutor question after the last task came back only as "the admitted
    # backend call failed"; the person could not know why.
    from spaces.learning.contracts.events import LearningToolName
    from spaces.learning.contracts.mcp_models import ActorV1, EventEnvelopeV1, ToolRequestV1
    from spaces.learning.mcp.tools.tutor import TutorGateway

    class Done(SessionStub):
        def current(self, session_id: str, *, actor_id: str) -> SessionTurn:
            return super().current(session_id, actor_id=actor_id).model_copy(
                update={"state": "completed", "task": None}
            )

    class NeverAsked:
        async def ask(self, **kwargs):
            raise AssertionError("backend must not be asked without an open task")

    request = ToolRequestV1(
        tool=LearningToolName.TUTOR_ASK,
        event=EventEnvelopeV1(
            event_type="learning.tutor.ask",
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            actor=ActorV1(actor_id="student-1", actor_type="local_user"),
            course_id=uuid4(),
            session_id=uuid4(),
            payload={"question": "Warum?"},
        ),
    )
    outcome = TutorGateway(sessions=Done(), tutor=NeverAsked()).execute(request)

    assert outcome.state == "rejected"
    assert outcome.error is not None and outcome.error.code == "no_open_task"
