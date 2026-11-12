from __future__ import annotations

import base64
import binascii
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
# Group capacity: the Huelle's grid and height rule, shared (pure modules, no Huelle runtime).
from spaces.learning.huelle.anordnung import gruppen_kapazitaet
from spaces.learning.huelle.medien import aufgaben_hoehen
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    AnswerCommand,
    PublicTask,
    SessionStartCommand,
    SessionTurn,
    VorbewertungCommand,
)
from spaces.learning.services.course_factory.model_gateway import png_bild
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.services.notizen.aus_bewertung import notiz_aus_bewertung
from spaces.learning.services.notizen.store import NoteStore

logger = logging.getLogger(__name__)

# 5 MB of PNG (model_gateway cap) is 4 * ceil(5_000_000 / 3) base64 characters.
_MAX_PNG_BASE64_CHARS = 6_666_668
_PREVIEW_KEYS = frozenset(
    {"handschrift_png_base64", "answer", "korrigiert_aus", "attempt_id"}
)


SESSION_TOOL_NAMES = frozenset(
    {
        LearningToolName.SESSION_START,
        LearningToolName.SESSION_RESUME,
        LearningToolName.COURSE_OVERVIEW,
        LearningToolName.TASK_NEXT,
        LearningToolName.TASK_LIST,
        LearningToolName.GROUP_CAPACITY,
        LearningToolName.TASK_ANSWER,
        LearningToolName.TASK_PREVIEW,
        LearningToolName.TASK_GRADING_DATA,
        LearningToolName.TASK_CONFIRM,
        LearningToolName.HINT_REQUEST,
        LearningToolName.PROGRESS_SHOW,
    }
)


class BewertungsNotizen:
    """Note hook shared by answers and canvas submissions.

    A wrong training answer leaves one note per (session, task); it never blocks the reply.
    `vorher` reads the task being answered (the turn after an answer carries the NEXT task),
    `nachher` writes the note from the turn's feedback.
    """

    def __init__(self, service: AdaptiveSessionService, notes: NoteStore) -> None:
        self._service = service
        self._notes = notes

    def vorher(
        self, session_id: str, actor_id: str, attempt_id: str | None = None
    ) -> PublicTask | None:
        try:
            if attempt_id is not None:
                # List sessions: the answered task is the one of this attempt, not the first open one.
                for eintrag in self._service.aufgabenliste(session_id, actor_id=actor_id):
                    if eintrag.attempt_id == attempt_id:
                        return eintrag.task
                return None
            return self._service.current(session_id, actor_id=actor_id).task
        except Exception:  # noqa: BLE001 - the answer itself reports the real error to the caller
            return None

    def nachher(self, turn: SessionTurn, task: PublicTask, actor_id: str) -> None:
        if turn.mode != "training" or turn.feedback is None:
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


class AdaptiveSessionGateway:
    def __init__(
        self, service: AdaptiveSessionService, notes: NoteStore | None = None
    ) -> None:
        self._service = service
        self._notes = BewertungsNotizen(service, notes) if notes is not None else None

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
            if request.tool is LearningToolName.TASK_PREVIEW:
                return self._preview(request)
            if request.tool is LearningToolName.TASK_GRADING_DATA:
                return self._grading_data(request)
            if request.tool is LearningToolName.TASK_CONFIRM:
                return self._turn_outcome(self._confirm(request))
            if request.tool is LearningToolName.TASK_NEXT:
                return self._turn_outcome(self._current(request))
            if request.tool is LearningToolName.TASK_LIST:
                return self._list(request)
            if request.tool is LearningToolName.GROUP_CAPACITY:
                return self._group_capacity(request)
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
        ablauf = payload.get("ablauf", "anpassend")
        blueprint = payload.get("blueprint")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError("session start requires course_revision")
        if mode not in {"training", "exam"} or not isinstance(blueprint, dict):
            raise ValueError("session start mode or blueprint is invalid")
        if ablauf not in {"anpassend", "liste"}:
            raise ValueError("session start ablauf is invalid")
        return self._service.start(
            SessionStartCommand(
                course_id=str(course_id),
                course_revision=revision,
                actor_id=request.event.actor.actor_id,
                mode=mode,
                blueprint=blueprint,
                ablauf=ablauf,
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
        attempt_id = _attempt_id(request.event.payload)
        actor_id = request.event.actor.actor_id
        # The turn after an answer carries the NEXT task, so the answered one is read first.
        answered = (
            self._notes.vorher(str(session_id), actor_id, attempt_id) if self._notes else None
        )
        turn = self._service.answer(
            AnswerCommand(
                session_id=str(session_id),
                actor_id=actor_id,
                expected_session_revision=revision,
                answer=answer,
                attempt_id=attempt_id,
            )
        )
        if answered is not None:
            self._note_from_feedback(turn, answered, actor_id)
        return turn

    def _preview(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        session_id = request.event.session_id
        revision = request.event.expected_revision
        if session_id is None or revision is None:
            raise ValueError("task preview requires session and revision")
        actor_id = request.event.actor.actor_id
        answer, png, korrigiert_aus = _preview_payload(request.event.payload)
        attempt_id = _attempt_id(request.event.payload)
        try:
            vorbewertung = self._service.vorbewerten(
                VorbewertungCommand(
                    session_id=str(session_id),
                    actor_id=actor_id,
                    expected_session_revision=revision,
                    answer=answer,
                    handschrift_png=png,
                    korrigiert_aus=korrigiert_aus,
                    attempt_id=attempt_id,
                )
            )
        except PersistenceConflict:
            raise  # a RuntimeError subclass, mapped by execute()
        except RuntimeError as error:
            # Reader or rubric evaluator is not configured / not reachable: retry later.
            logger.warning("learning_preview_unavailable", extra={"error_type": type(error).__name__})
            return ApplicationOutcomeV1(
                state="unavailable",
                error=ToolErrorV1(
                    code="session_grading_unavailable",
                    message="the preview grading is unavailable",
                    retryable=True,
                ),
            )
        turn = self._service.current(str(session_id), actor_id=actor_id)
        return self._turn_outcome(
            turn,
            extra={"vorbewertung": vorbewertung.model_dump(mode="json")},
            intent=False,
        )

    def _grading_data(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        """Model solution and rubric of the open attempt, for the Huelle only. Read-only; no exams."""
        session_id = request.event.session_id
        revision = request.event.expected_revision
        if session_id is None or revision is None:
            raise ValueError("grading data requires session and revision")
        payload = request.event.payload
        if not set(payload) <= {"attempt_id"}:
            raise ValueError("grading data payload has unknown fields")
        daten = self._service.bewertungsdaten(
            str(session_id), request.event.actor.actor_id, revision, _attempt_id(payload)
        )
        return ApplicationOutcomeV1(
            state="completed",
            aggregate=AggregateRefV1(
                aggregate_type="session", aggregate_id=str(session_id), revision=revision
            ),
            result=daten.model_dump(mode="json"),
        )

    def _confirm(self, request: ToolRequestV1) -> SessionTurn:
        session_id = request.event.session_id
        revision = request.event.expected_revision
        payload = request.event.payload
        vorbewertung_id = payload.get("vorbewertung_id")
        if (
            session_id is None
            or revision is None
            or not set(payload) <= {"vorbewertung_id", "attempt_id"}
            or not isinstance(vorbewertung_id, str)
        ):
            raise ValueError("task confirm requires session, revision, and vorbewertung_id")
        attempt_id = _attempt_id(payload)
        actor_id = request.event.actor.actor_id
        answered = (
            self._notes.vorher(str(session_id), actor_id, attempt_id) if self._notes else None
        )
        turn = self._service.bestaetigen(
            session_id=str(session_id),
            actor_id=actor_id,
            expected_session_revision=revision,
            vorbewertung_id=vorbewertung_id,
            attempt_id=attempt_id,
        )
        if answered is not None:
            self._note_from_feedback(turn, answered, actor_id)
        return turn

    def _note_from_feedback(
        self, turn: SessionTurn, task: PublicTask, actor_id: str
    ) -> None:
        """A wrong training answer leaves one note per (session, task); never blocks the reply."""
        if self._notes is not None:
            self._notes.nachher(turn, task, actor_id)

    def _list(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        turn = self._current(request)
        eintraege = self._service.aufgabenliste(
            turn.session_id, actor_id=request.event.actor.actor_id
        )
        return self._turn_outcome(
            turn,
            extra={"aufgaben": [e.model_dump(mode="json") for e in eintraege]},
            intent=False,
        )

    def _group_capacity(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        """How many folders (Mappen) a NEW group canvas of the course holds: the task list a new list session
        would get, heights by the Huelle's rule, places by its new grid. Read-only, any actor."""
        course_id = request.event.course_id
        if course_id is None:
            raise ValueError("group capacity requires course_id")
        revision, tasks = self._service.listen_aufgaben(course_id=str(course_id))
        heights = aufgaben_hoehen(task.model_dump(mode="json") for task in tasks)
        return self._course_outcome(
            str(course_id),
            revision,
            {"kapazitaet": gruppen_kapazitaet(heights), "aufgaben": len(tasks), "course_revision": revision},
        )

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


def _preview_payload(
    payload: Mapping[str, object],
) -> tuple[dict[str, object] | None, bytes | None, str | None]:
    """Exactly one of a typed answer and a handwriting PNG; anything else is invalid."""
    if not set(payload) <= _PREVIEW_KEYS:
        raise ValueError("task preview payload has unknown fields")
    encoded = payload.get("handschrift_png_base64")
    answer = payload.get("answer")
    korrigiert_aus = payload.get("korrigiert_aus")
    if korrigiert_aus is not None and not isinstance(korrigiert_aus, str):
        raise ValueError("korrigiert_aus must be a string")
    if (encoded is None) == (answer is None):
        raise ValueError("task preview needs exactly one of handschrift_png_base64 and answer")
    if encoded is not None:
        if korrigiert_aus is not None:
            raise ValueError("korrigiert_aus belongs to a typed answer")
        return None, _decode_png(encoded), None
    if not isinstance(answer, dict):
        raise ValueError("answer must be an object")
    return answer, None, korrigiert_aus


def _attempt_id(payload: Mapping[str, object]) -> str | None:
    value = payload.get("attempt_id")
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("attempt_id must be a non-empty string")
    return value


def _decode_png(encoded: object) -> bytes:
    """Length cap first, strict decode, then the shared PNG check; the image is never logged."""
    if not isinstance(encoded, str) or not encoded or len(encoded) > _MAX_PNG_BASE64_CHARS:
        raise ValueError("handschrift_png_base64 is invalid")
    try:
        png = base64.b64decode(encoded, validate=True)
        png_bild(png)
    except (binascii.Error, ValueError) as error:
        raise ValueError("handschrift_png_base64 is not a valid PNG") from error
    return png


def _rejected(code: str, message: str) -> ApplicationOutcomeV1:
    return ApplicationOutcomeV1(
        state="rejected",
        error=ToolErrorV1(code=code, message=message, retryable=False),
    )
