from __future__ import annotations

import logging
from collections.abc import Mapping
from uuid import UUID

from spaces.learning.bridge.dispatcher import ApplicationGateway, ApplicationOutcomeV1
from spaces.learning.contracts.events import LearningToolName
from spaces.learning.contracts.mcp_models import ToolRequestV1
from spaces.learning.contracts.outcomes import (
    AggregateRefV1,
    EvidenceRefV1,
    ToolErrorV1,
    TruthReadbackV1,
)
from spaces.learning.contracts.ui_intents import (
    OpenTaskIntentV1,
    ShowProgressIntentV1,
    ShowResultIntentV1,
)
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    AnswerCommand,
    PublicTask,
    SessionStartCommand,
    SessionTurn,
)
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.services.notizen.aus_bewertung import notiz_aus_bewertung
from spaces.learning.services.notizen.store import NoteStore

logger = logging.getLogger(__name__)


SESSION_TOOL_NAMES = frozenset(
    {
        LearningToolName.SESSION_START,
        LearningToolName.SESSION_RESUME,
        LearningToolName.COURSE_OVERVIEW,
        LearningToolName.TASK_NEXT,
        LearningToolName.TASK_ANSWER,
        LearningToolName.HINT_REQUEST,
        LearningToolName.PROGRESS_SHOW,
    }
)


class AdaptiveSessionGateway:
    def __init__(
        self, service: AdaptiveSessionService, notes: NoteStore | None = None
    ) -> None:
        self._service = service
        self._notes = notes

    def execute(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        try:
            if request.tool is LearningToolName.SESSION_START:
                turn = self._start(request)
                return self._turn_outcome(turn)
            if request.tool is LearningToolName.SESSION_RESUME:
                return self._resume(request)
            if request.tool is LearningToolName.COURSE_OVERVIEW:
                return self._overview(request)
            if request.tool is LearningToolName.TASK_ANSWER:
                turn = self._answer(request)
                return self._turn_outcome(turn)
            if request.tool is LearningToolName.TASK_NEXT:
                return self._turn_outcome(self._current(request))
            if request.tool is LearningToolName.HINT_REQUEST:
                turn = self._current(request)
                hint = self._service.hint(
                    turn.session_id, actor_id=request.event.actor.actor_id
                )
                return self._turn_outcome(turn, extra={"hint": hint}, intent=False)
            if request.tool is LearningToolName.PROGRESS_SHOW:
                turn = self._current(request)
                return self._turn_outcome(
                    turn,
                    extra={"progress": turn.progress.model_dump(mode="json")},
                    progress_intent=True,
                )
        except PersistenceConflict as error:
            code = (
                "session_revision_conflict"
                if "revision" in str(error)
                else "session_state_conflict"
            )
            return _rejected(code, str(error))
        except PermissionError as error:
            return _rejected("session_access_denied", str(error))
        except (LookupError, TypeError, ValueError) as error:
            return _rejected("invalid_session_request", str(error))
        raise ValueError("unsupported adaptive session tool")

    def readback(
        self, request: ToolRequestV1, outcome: ApplicationOutcomeV1
    ) -> TruthReadbackV1 | None:
        if outcome.aggregate is None:
            return None
        if outcome.aggregate.aggregate_type == "course":
            # Read-only course views have no session to re-read; the aggregate
            # names the course revision the answer was computed from.
            return TruthReadbackV1(
                invocation_id=request.event.invocation_id,
                correlation_id=request.event.correlation_id,
                owner="adaptive-engine",
                terminal_state="completed",
                aggregate=outcome.aggregate,
                evidence=EvidenceRefV1(
                    owner="adaptive-engine",
                    evidence_id=(
                        f"course:{outcome.aggregate.aggregate_id}:"
                        f"{outcome.aggregate.revision}"
                    ),
                    evidence_type="application_readback",
                ),
            )
        if not self._service.has_revision(
            outcome.aggregate.aggregate_id, outcome.aggregate.revision
        ):
            return None
        return TruthReadbackV1(
            invocation_id=request.event.invocation_id,
            correlation_id=request.event.correlation_id,
            owner="adaptive-engine",
            terminal_state="completed",
            aggregate=outcome.aggregate,
            evidence=EvidenceRefV1(
                owner="adaptive-engine",
                evidence_id=(
                    f"session:{outcome.aggregate.aggregate_id}:"
                    f"{outcome.aggregate.revision}"
                ),
                evidence_type="application_readback",
            ),
        )

    def _start(self, request: ToolRequestV1) -> SessionTurn:
        course_id = request.event.course_id
        if course_id is None:
            raise ValueError("session start requires course_id")
        payload = request.event.payload
        revision = payload.get("course_revision")
        mode = payload.get("mode")
        blueprint = payload.get("blueprint")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError("session start requires course_revision")
        if mode not in {"training", "exam"} or not isinstance(blueprint, dict):
            raise ValueError("session start mode or blueprint is invalid")
        return self._service.start(
            SessionStartCommand(
                course_id=str(course_id),
                course_revision=revision,
                actor_id=request.event.actor.actor_id,
                mode=mode,
                blueprint=blueprint,
            )
        )

    def _resume(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        course_id = request.event.course_id
        revision = request.event.payload.get("course_revision")
        if course_id is None:
            raise ValueError("session resume requires course_id")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError("session resume requires course_revision")
        turn = self._service.resume(
            course_id=str(course_id),
            course_revision=revision,
            actor_id=request.event.actor.actor_id,
        )
        if turn is not None:
            return self._turn_outcome(turn, intent=False)
        return self._course_outcome(str(course_id), revision, {"session": None})

    def _overview(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        course_id = request.event.course_id
        if course_id is None:
            raise ValueError("course overview requires course_id")
        overview = self._service.ueberblick(
            course_id=str(course_id), actor_id=request.event.actor.actor_id
        )
        return self._course_outcome(
            str(course_id), overview["course_revision"] or 0, overview
        )

    @staticmethod
    def _course_outcome(
        course_id: str, revision: int, result: dict[str, object]
    ) -> ApplicationOutcomeV1:
        return ApplicationOutcomeV1(
            state="completed",
            aggregate=AggregateRefV1(
                aggregate_type="course", aggregate_id=course_id, revision=revision
            ),
            result=result,
        )

    def _answer(self, request: ToolRequestV1) -> SessionTurn:
        session_id = request.event.session_id
        revision = request.event.expected_revision
        answer = request.event.payload.get("answer")
        if session_id is None or revision is None or not isinstance(answer, dict):
            raise ValueError("task answer requires session, revision, and answer")
        actor_id = request.event.actor.actor_id
        # The turn after an answer carries the NEXT task, so the answered one is read first.
        answered = self._answered_task(str(session_id), actor_id) if self._notes else None
        turn = self._service.answer(
            AnswerCommand(
                session_id=str(session_id),
                actor_id=actor_id,
                expected_session_revision=revision,
                answer=answer,
            )
        )
        if answered is not None:
            self._note_from_feedback(turn, answered, actor_id)
        return turn

    def _answered_task(self, session_id: str, actor_id: str) -> PublicTask | None:
        try:
            return self._service.current(session_id, actor_id=actor_id).task
        except Exception:  # noqa: BLE001 - answer() reports the real error to the caller
            return None

    def _note_from_feedback(
        self, turn: SessionTurn, task: PublicTask, actor_id: str
    ) -> None:
        """A wrong training answer leaves one note per (session, task); never blocks the reply."""
        if turn.mode != "training" or turn.feedback is None or self._notes is None:
            return
        try:
            fields = notiz_aus_bewertung(task, turn.feedback, turn.session_id)
            if fields is None:
                return
            anchor = fields["anchor"]
            if any(
                n["sequence_id"] == turn.session_id and n["anchor"] == anchor
                for n in self._notes.list_for(actor_id, quelle="bewertung")
            ):
                return
            self._notes.create(actor_id=actor_id, course_id=turn.course_id, **fields)
        except Exception as error:  # noqa: BLE001 - the learner reply stays unchanged
            logger.warning("learning_note_from_rating_failed", extra={"error_type": type(error).__name__})

    def _current(self, request: ToolRequestV1) -> SessionTurn:
        session_id = request.event.session_id
        if session_id is None:
            raise ValueError("session operation requires session_id")
        return self._service.current(
            str(session_id), actor_id=request.event.actor.actor_id
        )

    @staticmethod
    def _turn_outcome(
        turn: SessionTurn,
        *,
        extra: dict[str, object] | None = None,
        intent: bool = True,
        progress_intent: bool = False,
    ) -> ApplicationOutcomeV1:
        aggregate = AggregateRefV1(
            aggregate_type="session",
            aggregate_id=turn.session_id,
            revision=turn.session_revision,
        )
        result: dict[str, object] = {"session": turn.model_dump(mode="json")}
        if extra:
            result.update(extra)
        ui_intent = None
        if progress_intent:
            ui_intent = ShowProgressIntentV1(
                aggregate_id=turn.session_id,
                aggregate_revision=turn.session_revision,
                course_id=UUID(turn.course_id),
            )
        elif intent and turn.task is not None:
            ui_intent = OpenTaskIntentV1(
                aggregate_id=turn.session_id,
                aggregate_revision=turn.session_revision,
                session_id=UUID(turn.session_id),
                task_id=UUID(turn.task.id),
            )
        elif intent and turn.summary is not None:
            result_id = turn.audit.evaluation_id or turn.session_id
            ui_intent = ShowResultIntentV1(
                aggregate_id=turn.session_id,
                aggregate_revision=turn.session_revision,
                result_id=UUID(result_id),
            )
        return ApplicationOutcomeV1(
            state="completed",
            aggregate=aggregate,
            result=result,
            ui_intent=ui_intent,
        )


def build_session_gateways(
    service: AdaptiveSessionService,
    notes: NoteStore | None = None,
) -> Mapping[LearningToolName, ApplicationGateway]:
    gateway = AdaptiveSessionGateway(service, notes)
    return {tool: gateway for tool in SESSION_TOOL_NAMES}


def _rejected(code: str, message: str) -> ApplicationOutcomeV1:
    return ApplicationOutcomeV1(
        state="rejected",
        error=ToolErrorV1(code=code, message=message, retryable=False),
    )
