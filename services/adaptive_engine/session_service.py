from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spaces.learning.services.adaptive_engine.eligibility import (
    CandidateItem,
    SelectionContext,
)
from spaces.learning.services.adaptive_engine.mastery import (
    MasteryState,
    calculate_mastery_update,
)
from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ConceptMastery,
    ItemConcept,
    LearningAttempt,
    LearningEvaluation,
    LearningResponse,
    LearningSession,
    LearningVorbewertung,
    MasteryEvidence,
    Misconception,
    ReviewSchedule,
    LearningReviewDecision,
    LearningReviewItem,
)
from spaces.learning.services.adaptive_engine.review_schedule import (
    ReviewState,
    schedule_review,
)
from spaces.learning.services.adaptive_engine.selector import select_next_item
from spaces.learning.services.course_factory.repository import SessionFactory
from spaces.learning.services.db.repository import PersistenceConflict
from spaces.learning.services.evaluation.deterministic import score_response
from spaces.learning.services.evaluation.schemas import (
    ChoiceOption,
    DeterministicItem,
    RubricCriterion,
    RubricDecision,
    RubricEvaluationRequest,
)
from spaces.learning.services import zufall

if TYPE_CHECKING:
    from spaces.learning.services.evaluation.handschrift import GelesenV1


_DETERMINISTIC_TYPES = frozenset(
    {"single_choice", "multiple_choice", "matching", "ordering", "numeric", "fill_in"}
)
_RUBRIC_TYPES = frozenset({"open", "case", "code"})
# Rubric types a handwritten preview is graded for. penecho_canvas belongs here even when
# its drawing submission runs through the external canvas path.
_HANDSCHRIFT_RUBRIC_TYPES = frozenset({"open", "case", "penecho_canvas"})
# Review reason when an unaccepted rubric decision carries no rationale code.
_FALLBACK_REVIEW_REASON = "rubric_not_accepted"
# From this best score an item counts as solved for selection (a sketch graded 0.97 must
# not be re-served). Feedback bands in _feedback stay at 0.999 / 0.65.
_SOLVED_SCORE = 0.9


class SessionRubricBoundary(Protocol):
    def evaluate(
        self, request: RubricEvaluationRequest, *, bild_png: bytes | None = None
    ) -> RubricDecision: ...


class SessionLeseBoundary(Protocol):
    def lesen(
        self, task: dict[str, object], png: bytes
    ) -> tuple[GelesenV1, dict[str, object] | None]: ...


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionStartCommand(_Command):
    course_id: str
    course_revision: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=128)
    mode: Literal["training", "exam"]
    blueprint: dict[str, object]

    @field_validator("course_id")
    @classmethod
    def validate_course_id(cls, value: str) -> str:
        return str(UUID(value))


class AnswerCommand(_Command):
    session_id: str
    actor_id: str = Field(min_length=1, max_length=128)
    expected_session_revision: int = Field(ge=1)
    answer: dict[str, object]

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        return str(UUID(value))


class VorbewertungCommand(_Command):
    """A preview grading: exactly one of a typed answer and a handwriting PNG.

    `korrigiert_aus` ("Falsch gelesen") is a typed answer that replaces the reading of
    an earlier preview of the same attempt.
    """

    session_id: str
    actor_id: str = Field(min_length=1, max_length=128)
    expected_session_revision: int = Field(ge=1)
    answer: dict[str, object] | None = None
    handschrift_png: bytes | None = Field(default=None, min_length=1)
    korrigiert_aus: str | None = None

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("korrigiert_aus")
    @classmethod
    def validate_korrigiert_aus(cls, value: str | None) -> str | None:
        return None if value is None else str(UUID(value))

    @model_validator(mode="after")
    def validate_antwortform(self) -> "VorbewertungCommand":
        if (self.answer is None) == (self.handschrift_png is None):
            raise ValueError("give exactly one of answer and handschrift_png")
        if self.korrigiert_aus is not None and self.answer is None:
            raise ValueError("korrigiert_aus needs a typed answer")
        return self


class PublicTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    type: str
    prompt: str
    options: tuple[ChoiceOption, ...]
    difficulty: float = Field(ge=0, le=1)
    representation: str
    source_refs: tuple[str, ...]
    presentation: dict[str, object]
    expected_answer: None = None


class SessionProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed: int = Field(ge=0)
    total: int = Field(ge=1)


class CriterionFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str
    description: str
    score: float = Field(ge=0, le=1)
    feedback: str = ""


class TurnFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["correct", "incorrect", "partial", "needs_review"]
    score: float = Field(ge=0, le=1)
    rationale_codes: tuple[str, ...]
    source_refs: tuple[str, ...]
    # Per-criterion verdicts of rubric types; empty for deterministic types and not
    # persisted (a resumed session carries no feedback at all).
    criteria: tuple[CriterionFeedback, ...] = ()


class Vorbewertung(BaseModel):
    """A stored preview grading; nothing of it counts until it is confirmed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    attempt_id: str
    quelle: Literal["handschrift", "getippt"]
    lesbar: bool
    gelesen_text: str | None
    art: Literal["deterministic", "rubric"]
    score: float | None
    accepted: bool | None
    feedback: TurnFeedback | None


class AuditReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    response_id: str | None = None
    evaluation_id: str | None = None
    mastery_applied: bool = False
    readback_verified: bool = False


class ExamResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int
    item_id: str
    score: float


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempted: int
    correct: int
    score: float = Field(ge=0, le=1)
    results: tuple[ExamResult, ...]


class SessionTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    course_id: str
    session_revision: int
    state: Literal["active", "completed", "cancelled"]
    mode: Literal["training", "exam"]
    task: PublicTask | None
    progress: SessionProgress
    feedback: TurnFeedback | None = None
    summary: SessionSummary | None = None
    audit: AuditReceipt = AuditReceipt()


@dataclass(frozen=True)
class _AnswerAudit:
    response_id: str
    evaluation_id: str
    mastery_applied: bool
    criteria: tuple[CriterionFeedback, ...] = ()


@dataclass(frozen=True)
class _Bewertung:
    """A graded answer that is not persisted yet; the ids are fixed at grading time."""

    response_id: str
    evaluation_id: str
    answer: dict[str, object]
    evaluator_type: str
    score: float
    confidence: float
    accepted: bool
    rationale_codes: tuple[str, ...]
    evaluator_versions: dict[str, object]
    source_refs: tuple[str, ...]
    criteria: tuple[CriterionFeedback, ...]
    review_reason: str | None
    review_details: dict[str, object]


class AdaptiveSessionService:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rubric_evaluator: SessionRubricBoundary | None = None,
        external_rubric_types: frozenset[str] = frozenset(),
        handschrift_leser: SessionLeseBoundary | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._rubric_evaluator = rubric_evaluator
        self._handschrift_leser = handschrift_leser
        if not external_rubric_types <= {"penecho_canvas"}:
            raise ValueError("adaptive external rubric type is not authorized")
        self._external_rubric_types = external_rubric_types

    def start(self, command: SessionStartCommand) -> SessionTurn:
        now = self._aware_now()
        with self._session_factory() as db, db.begin():
            items = self._catalog(db, command.course_id, command.course_revision)
            if not items:
                raise LookupError("adaptive course revision has no approved items")
            blueprint = self._normalize_blueprint(command, items)
            row = LearningSession(
                id=str(uuid4()),
                course_id=command.course_id,
                course_revision=command.course_revision,
                actor_id=command.actor_id,
                mode=command.mode,
                state="active",
                revision=1,
                blueprint=blueprint,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            db.add(row)
            db.flush()
            self._append_next(db, row, items)
            turn = self._turn(db, row)
        return self._with_readback(turn)

    def resume(
        self, *, course_id: str, course_revision: int | None = None, actor_id: str
    ) -> SessionTurn | None:
        """The newest active TRAINING session of the person in this course, of any revision.

        A new course revision must not orphan a running practice session, and exam sessions are
        never resumed. `course_revision` is still accepted (callers send it) but does not filter;
        new sessions keep taking their revision from the caller.
        """
        del course_revision
        course_id = str(UUID(course_id))
        with self._session_factory() as db:
            row = db.scalar(
                select(LearningSession)
                .where(
                    LearningSession.course_id == course_id,
                    LearningSession.actor_id == actor_id,
                    LearningSession.mode == "training",
                    LearningSession.state == "active",
                )
                .order_by(LearningSession.created_at.desc(), LearningSession.id.desc())
                .limit(1)
            )
            return self._with_readback(self._turn(db, row)) if row is not None else None

    def ueberblick(self, *, course_id: str, actor_id: str) -> dict[str, object]:
        """Begriffe mit Beherrschung der Person und die aktive Sitzung."""
        course_id = str(UUID(course_id))
        with self._session_factory() as db:
            revision = db.scalar(
                select(func.max(AdaptiveItem.course_revision)).where(
                    AdaptiveItem.course_id == course_id,
                    AdaptiveItem.quality_status == "approved",
                )
            )
            if revision is None:
                return {
                    "course_id": course_id,
                    "course_revision": None,
                    "begriffe": [],
                    "session": None,
                }
            rows = db.execute(
                select(AdaptiveConcept, ConceptMastery)
                .outerjoin(
                    ConceptMastery,
                    (ConceptMastery.concept_id == AdaptiveConcept.id)
                    & (ConceptMastery.actor_id == actor_id),
                )
                .where(
                    AdaptiveConcept.course_id == course_id,
                    AdaptiveConcept.course_revision == revision,
                )
            ).all()
            begriffe = sorted(
                (
                    {
                        "concept_key": concept.concept_key,
                        "titel": concept.title,
                        "beherrschung": (
                            None
                            if mastery is None
                            else round(mastery.alpha / (mastery.alpha + mastery.beta), 2)
                        ),
                    }
                    for concept, mastery in rows
                ),
                key=lambda entry: (str(entry["titel"]), str(entry["concept_key"])),
            )
        turn = self.resume(
            course_id=course_id, course_revision=revision, actor_id=actor_id
        )
        return {
            "course_id": course_id,
            "course_revision": revision,
            "begriffe": begriffe,
            "session": turn.model_dump(mode="json") if turn is not None else None,
        }

    def current(self, session_id: str, *, actor_id: str) -> SessionTurn:
        with self._session_factory() as db:
            row = db.get(LearningSession, str(UUID(session_id)))
            self._assert_owned(row, actor_id)
            assert row is not None
            return self._with_readback(self._turn(db, row))

    def has_revision(self, session_id: str, revision: int) -> bool:
        with self._session_factory() as db:
            row = db.get(LearningSession, str(UUID(session_id)))
            return row is not None and row.revision == revision

    def answer(self, command: AnswerCommand) -> SessionTurn:
        now = self._aware_now()
        with self._session_factory() as db, db.begin():
            row = self._active_session(db, command.session_id, command.actor_id, lock=True)
            attempt, item = self._open_attempt(db, row, command.expected_session_revision)
            bewertung = self._bewerten(item, command.answer)
            turn = self._book(db, row, attempt, item, bewertung, now)
        return self._with_readback(turn)

    def vorbewerten(self, command: VorbewertungCommand) -> Vorbewertung:
        """Grade the current attempt as a preview: stored, but no learning state changes.

        The model call (reader or rubric) runs outside any transaction; the session is
        checked again before the preview is stored. The revision is never changed here.
        """
        with self._session_factory() as db:
            row = self._active_session(db, command.session_id, command.actor_id, lock=False)
            if row.mode == "exam":
                raise PermissionError("exam sessions have no preview grading")
            attempt, item = self._open_attempt(db, row, command.expected_session_revision)
            rubrik = self._vorschau_rubrik(item, command)
            if command.korrigiert_aus is not None:
                korrigiert = db.get(LearningVorbewertung, command.korrigiert_aus)
                if korrigiert is None or korrigiert.attempt_id != attempt.id:
                    raise ValueError("korrigiert_aus must name a preview of this attempt")
            attempt_id = attempt.id
            task = self._public_task(item, row.id).model_dump(mode="json")
        png = command.handschrift_png
        sha = hashlib.sha256(png).hexdigest() if png is not None else None
        vorbewertung_id = str(uuid4())
        gelesen: GelesenV1 | None = None
        bewertung: _Bewertung | None = None
        if rubrik:
            if self._rubric_evaluator is None:
                raise RuntimeError("rubric evaluator is not configured")
            # The booked answer refers to this preview, which keeps the PNG for review.
            bewertung = self._bewerten(
                item,
                {"kind": "handschrift", "vorbewertung_id": vorbewertung_id, "bild_sha256": sha},
                bild_png=png,
            )
        elif png is not None:
            if self._handschrift_leser is None:
                raise RuntimeError("handwriting reader is not configured")
            gelesen, antwort = self._handschrift_leser.lesen(task, png)
            if gelesen.lesbar and antwort is not None:
                try:
                    bewertung = self._bewerten(item, antwort)
                except ValueError:
                    # A reading the scorer cannot take counts as unreadable.
                    bewertung = None
        else:
            assert command.answer is not None
            bewertung = self._bewerten(item, command.answer)
        now = self._aware_now()
        with self._session_factory() as db, db.begin():
            row = self._active_session(db, command.session_id, command.actor_id, lock=True)
            attempt, _ = self._open_attempt(db, row, command.expected_session_revision)
            if attempt.id != attempt_id:
                raise PersistenceConflict("vorbewertung not current")
            stored = LearningVorbewertung(
                id=vorbewertung_id,
                attempt_id=attempt.id,
                session_id=row.id,
                quelle="handschrift" if png is not None else "getippt",
                korrigiert_aus=command.korrigiert_aus,
                bild_sha256=sha,
                bild_png=png if rubrik else None,
                gelesen=gelesen.model_dump(mode="json") if gelesen is not None else None,
                lesbar=bewertung is not None,
                created_at=now,
                **self._bewertung_spalten(bewertung),
            )
            db.add(stored)
            db.flush()
        return Vorbewertung(
            id=stored.id,
            attempt_id=stored.attempt_id,
            quelle=stored.quelle,
            lesbar=stored.lesbar,
            gelesen_text=gelesen.text if gelesen is not None else None,
            art="rubric" if rubrik else "deterministic",
            score=bewertung.score if bewertung is not None else None,
            accepted=bewertung.accepted if bewertung is not None else None,
            feedback=self._feedback_aus(bewertung) if bewertung is not None else None,
        )

    def bestaetigen(
        self,
        *,
        session_id: str,
        actor_id: str,
        expected_session_revision: int,
        vorbewertung_id: str,
    ) -> SessionTurn:
        """Book exactly the stored preview ("Passt"): no new model call, the same ids."""
        now = self._aware_now()
        with self._session_factory() as db, db.begin():
            row = self._active_session(db, str(UUID(session_id)), actor_id, lock=True)
            if row.mode == "exam":
                raise PermissionError("exam sessions have no preview grading")
            if row.revision != expected_session_revision:
                raise PersistenceConflict("adaptive session revision conflict")
            stored = db.get(LearningVorbewertung, str(UUID(vorbewertung_id)))
            if stored is None or stored.session_id != row.id:
                raise LookupError("vorbewertung is unknown")
            attempt = self._current_attempt(db, row.id)
            if (
                attempt is None
                or stored.attempt_id != attempt.id
                or self._answered(db, attempt.id)
            ):
                raise PersistenceConflict("vorbewertung not current")
            if not stored.lesbar:
                raise ValueError("vorbewertung is not readable")
            item = db.get(AdaptiveItem, attempt.item_id)
            if item is None:
                raise LookupError("adaptive item is unavailable")
            turn = self._book(db, row, attempt, item, self._bewertung_aus(stored), now)
        return self._with_readback(turn)

    def _active_session(
        self, db: Session, session_id: str, actor_id: str, *, lock: bool
    ) -> LearningSession:
        row = db.get(LearningSession, session_id, with_for_update=lock)
        self._assert_owned_active(row, actor_id)
        assert row is not None
        return row

    def _open_attempt(
        self, db: Session, row: LearningSession, expected_revision: int
    ) -> tuple[LearningAttempt, AdaptiveItem]:
        """The current, unanswered attempt of a session at the expected revision."""
        if row.revision != expected_revision:
            raise PersistenceConflict("adaptive session revision conflict")
        attempt = self._current_attempt(db, row.id)
        if attempt is None:
            raise PersistenceConflict("adaptive session has no current task")
        if self._answered(db, attempt.id):
            raise PersistenceConflict("adaptive task was already answered")
        item = db.get(AdaptiveItem, attempt.item_id)
        if item is None:
            raise LookupError("adaptive item is unavailable")
        return attempt, item

    @staticmethod
    def _answered(db: Session, attempt_id: str) -> bool:
        return (
            db.scalar(
                select(LearningResponse.id).where(LearningResponse.attempt_id == attempt_id)
            )
            is not None
        )

    def _book(
        self,
        db: Session,
        row: LearningSession,
        attempt: LearningAttempt,
        item: AdaptiveItem,
        bewertung: _Bewertung,
        now: datetime,
    ) -> SessionTurn:
        """Persist a graded answer, advance the session and build the turn (answer/confirm)."""
        evaluation, audit = self._uebernehmen(db, row, attempt, item, bewertung, now)
        row.revision += 1
        row.updated_at = now
        total = self._total(row)
        if attempt.ordinal >= total:
            row.state = "completed"
            row.completed_at = now
        else:
            self._append_next(db, row, self._catalog(db, row.course_id, row.course_revision))
        db.flush()
        feedback = (
            self._feedback(evaluation, audit.criteria)
            if row.mode == "training"
            else None
        )
        return self._turn(db, row, feedback=feedback, answer_audit=audit)

    @staticmethod
    def _vorschau_rubrik(item: AdaptiveItem, command: VorbewertungCommand) -> bool:
        """Whether a preview of this item goes to the rubric; rejects what has no preview."""
        if item.item_type in _DETERMINISTIC_TYPES:
            return False
        if item.item_type in _HANDSCHRIFT_RUBRIC_TYPES:
            if command.handschrift_png is None:
                raise ValueError("rubric tasks are previewed from handwriting only")
            return True
        raise ValueError(f"{item.item_type} tasks have no preview grading")

    @staticmethod
    def _bewertung_spalten(bewertung: _Bewertung | None) -> dict[str, object]:
        """The columns of a stored preview grading; empty when nothing was graded."""
        if bewertung is None:
            return {}
        return {
            "answer": bewertung.answer,
            "response_id": bewertung.response_id,
            "evaluation_id": bewertung.evaluation_id,
            "evaluator_type": bewertung.evaluator_type,
            "score": bewertung.score,
            "confidence": bewertung.confidence,
            "accepted": bewertung.accepted,
            "rationale_codes": list(bewertung.rationale_codes),
            "evaluator_versions": bewertung.evaluator_versions,
            "source_refs": list(bewertung.source_refs),
            "criteria": [value.model_dump(mode="json") for value in bewertung.criteria],
            "review_reason": bewertung.review_reason,
            "review_details": bewertung.review_details,
        }

    @staticmethod
    def _bewertung_aus(stored: LearningVorbewertung) -> _Bewertung:
        """The grading exactly as it was stored with the preview."""
        if not stored.lesbar or stored.response_id is None or stored.evaluation_id is None:
            raise ValueError("vorbewertung is not readable")
        return _Bewertung(
            response_id=stored.response_id,
            evaluation_id=stored.evaluation_id,
            answer=dict(stored.answer or {}),
            evaluator_type=str(stored.evaluator_type),
            score=float(stored.score),
            confidence=float(stored.confidence),
            accepted=bool(stored.accepted),
            rationale_codes=tuple(stored.rationale_codes or ()),
            evaluator_versions=dict(stored.evaluator_versions or {}),
            source_refs=tuple(stored.source_refs or ()),
            criteria=tuple(
                CriterionFeedback.model_validate(value) for value in stored.criteria or ()
            ),
            review_reason=stored.review_reason,
            review_details=dict(stored.review_details or {}),
        )

    def hint(self, session_id: str, *, actor_id: str) -> str:
        with self._session_factory() as db:
            row = db.get(LearningSession, str(UUID(session_id)))
            self._assert_owned_active(row, actor_id)
            assert row is not None
            if row.mode == "exam":
                raise PermissionError("exam sessions do not expose hints")
            attempt = self._current_attempt(db, row.id)
            item = db.get(AdaptiveItem, attempt.item_id) if attempt else None
            hints = item.scoring_config.get("hints", []) if item is not None else []
            if not isinstance(hints, list) or not hints or not isinstance(hints[0], str):
                raise LookupError("no approved hint is available")
            return hints[0]

    def record_sequence_answer(
        self, *, actor_id: str, course_id: str, activity_id: str, selected: list[str]
    ) -> dict[str, object] | None:
        """A choice answered in the sequence player counts for the learner's mastery.

        The checkpoint comes from the same factory activity as the practice item; the
        engine scores it itself (deterministic) inside one "lernsequenz" session per
        learner and course revision. None when the course has no such choice item.
        """
        if not selected or len(selected) > 12:
            raise ValueError("choose at least one option")
        now = self._aware_now()
        with self._session_factory() as db, db.begin():
            item = db.scalars(
                select(AdaptiveItem)
                .where(
                    AdaptiveItem.course_id == course_id,
                    AdaptiveItem.activity_id == activity_id,
                    AdaptiveItem.item_type.in_(("single_choice", "multiple_choice")),
                )
                .order_by(AdaptiveItem.course_revision.desc())
            ).first()
            if item is None:
                return None
            row = db.scalars(
                select(LearningSession)
                .where(
                    LearningSession.actor_id == actor_id,
                    LearningSession.course_id == item.course_id,
                    LearningSession.course_revision == item.course_revision,
                    LearningSession.mode == "training",
                )
                .order_by(LearningSession.created_at.desc())
                .with_for_update()
            ).all()
            row = next((r for r in row if (r.blueprint or {}).get("source") == "lernsequenz"), None)
            if row is None:
                row = LearningSession(
                    id=str(uuid4()), course_id=item.course_id, course_revision=item.course_revision,
                    actor_id=actor_id, mode="training", state="active", revision=1,
                    blueprint={"source": "lernsequenz", "max_attempts": 10_000},
                    created_at=now, updated_at=now,
                )
                db.add(row)
                db.flush()
            ordinal = (db.scalar(
                select(func.max(LearningAttempt.ordinal)).where(LearningAttempt.session_id == row.id)
            ) or 0) + 1
            attempt = LearningAttempt(
                id=str(uuid4()), session_id=row.id, item_id=item.id, course_id=row.course_id,
                course_revision=row.course_revision, ordinal=ordinal,
                selection_reason={"source": "lernsequenz"}, selected_at=now,
            )
            db.add(attempt)
            row.revision += 1
            row.updated_at = now
            db.flush()
            answer: dict[str, object] = (
                {"choice_id": selected[0]} if item.item_type == "single_choice" else {"choice_ids": list(selected)}
            )
            bewertung = self._bewerten(item, answer)
            evaluation, audit = self._uebernehmen(db, row, attempt, item, bewertung, now)
            return {
                "score": evaluation.score,
                "mastery_applied": audit.mastery_applied,
                "evaluation_id": evaluation.id,
            }

    def pending_reviews(self, limit: int = 50) -> list[dict[str, object]]:
        """AI evaluations held for a person (low confidence, ungrounded feedback)."""
        with self._session_factory() as db:
            rows = db.execute(
                select(LearningReviewItem, LearningEvaluation, LearningResponse, AdaptiveItem,
                       LearningSession)
                .join(LearningEvaluation, LearningEvaluation.id == LearningReviewItem.evaluation_id)
                .join(LearningResponse, LearningResponse.id == LearningEvaluation.response_id)
                .join(LearningAttempt, LearningAttempt.id == LearningResponse.attempt_id)
                .join(AdaptiveItem, AdaptiveItem.id == LearningAttempt.item_id)
                .join(LearningSession, LearningSession.id == LearningAttempt.session_id)
                .where(LearningReviewItem.status == "pending")
                .order_by(LearningReviewItem.created_at)
                .limit(max(1, min(limit, 200)))
            ).all()
            return [_review_entry(*row) for row in rows]

    def resolve_review(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        decision: str,
        score: float | None = None,
        note: str | None = None,
    ) -> dict[str, object]:
        """A person confirms or overrides a held AI evaluation.

        The AI evaluation stays as evidence; a "manual" evaluation with the person's
        score is added, mastery is credited from it and the review is resolved.
        """
        if decision not in {"confirm", "override"}:
            raise ValueError("review decision must be confirm or override")
        if decision == "override" and (
            isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= 1
        ):
            raise ValueError("an override needs a score between 0 and 1")
        if not reviewer_id or len(reviewer_id) > 128:
            raise ValueError("reviewer is invalid")
        if note is not None and len(note) > 2_000:
            raise ValueError("review note is too long")
        now = self._aware_now()
        with self._session_factory() as db, db.begin():
            review = db.get(LearningReviewItem, str(UUID(review_id)), with_for_update=True)
            if review is None:
                raise LookupError("review is unknown")
            if review.status != "pending" or review.evaluation_id is None:
                raise PersistenceConflict("review is already decided")
            ai = db.get(LearningEvaluation, review.evaluation_id)
            response = db.get(LearningResponse, ai.response_id)
            attempt = db.get(LearningAttempt, response.attempt_id)
            session_row = db.get(LearningSession, attempt.session_id)
            final = float(ai.score if decision == "confirm" else score)
            human = LearningEvaluation(
                id=str(uuid4()),
                response_id=ai.response_id,
                evaluator_type="manual",
                score=final,
                confidence=1.0,
                accepted=True,
                rationale_codes=[
                    "human_review_confirmed" if decision == "confirm" else "human_review_override",
                    "answer_correct" if final >= 0.999
                    else "answer_partial" if final >= 0.65
                    else "answer_incorrect",
                ],
                evaluator_versions={"reviewer": reviewer_id, "decides": ai.id},
                source_refs=list(ai.source_refs),
                created_at=now,
            )
            db.add(human)
            db.flush()
            applied = self._apply_mastery_and_review(
                db,
                actor_id=session_row.actor_id,
                attempt=attempt,
                evaluation=human,
                successful=final >= 0.65,
                now=now,
            )
            review.status = "resolved"
            review.resolved_at = now
            db.add(
                LearningReviewDecision(
                    id=str(uuid4()),
                    review_item_id=review.id,
                    ai_evaluation_id=ai.id,
                    human_evaluation_id=human.id,
                    decision=decision,
                    reviewer_id=reviewer_id,
                    note=note,
                    created_at=now,
                )
            )
            db.flush()
            return {
                "review_id": review.id,
                "decision": decision,
                "score": final,
                "mastery_applied": applied,
                "human_evaluation_id": human.id,
            }

    def progress(self, session_id: str, *, actor_id: str) -> SessionProgress:
        with self._session_factory() as db:
            row = db.get(LearningSession, str(UUID(session_id)))
            self._assert_owned(row, actor_id)
            assert row is not None
            return self._progress(db, row)

    def _bewerten(
        self,
        item: AdaptiveItem,
        answer: dict[str, object],
        *,
        bild_png: bytes | None = None,
    ) -> _Bewertung:
        """Grade an answer without writing anything; creates the response/evaluation ids.

        `bild_png` (handwriting) goes to the rubric with the answer; for a handwritten
        preview penecho_canvas is graded here even when its drawing path is external.
        """
        if item.item_type in _DETERMINISTIC_TYPES:
            return self._bewerten_deterministic(item, answer)
        handschrift = bild_png is not None and item.item_type in _HANDSCHRIFT_RUBRIC_TYPES
        if (
            handschrift
            or item.item_type in _RUBRIC_TYPES
            or (
                item.item_type == "penecho_canvas"
                and "penecho_canvas" not in self._external_rubric_types
            )
        ) and self._rubric_evaluator is not None:
            return self._bewerten_rubric(item, answer, bild_png=bild_png)
        raise ValueError("adaptive item has no admitted evaluator")

    def _bewerten_deterministic(
        self, item: AdaptiveItem, answer: dict[str, object]
    ) -> _Bewertung:
        scored = score_response(
            DeterministicItem(
                item_id=item.id,
                item_type=item.item_type,
                prompt=self._string_config(item, "prompt"),
                options=self._options(item),
                expected_answer=item.expected_answer,
                scoring_config=item.scoring_config,
            ),
            answer,
        )
        return _Bewertung(
            response_id=str(uuid4()),
            evaluation_id=str(uuid4()),
            answer=answer,
            evaluator_type="deterministic",
            score=scored.score,
            confidence=scored.confidence,
            accepted=True,
            rationale_codes=tuple(scored.rationale_codes),
            evaluator_versions={"scorer": "deterministic-v1"},
            source_refs=self._string_list(item.scoring_config.get("source_refs", [])),
            criteria=(),
            review_reason=None,
            review_details={},
        )

    def _bewerten_rubric(
        self,
        item: AdaptiveItem,
        answer: dict[str, object],
        *,
        bild_png: bytes | None = None,
    ) -> _Bewertung:
        assert self._rubric_evaluator is not None
        response_id = str(uuid4())
        evaluation_id = str(uuid4())
        expected = item.expected_answer.get("text")
        if not isinstance(expected, str) or not expected:
            raise ValueError("rubric item expected answer is invalid")
        source_refs = self._string_list(item.scoring_config.get("source_refs", []))
        criteria = [RubricCriterion.model_validate(value) for value in item.rubric]
        request = RubricEvaluationRequest(
            evaluation_id=evaluation_id,
            response_id=response_id,
            response=answer,
            expected_answer=expected,
            rubric=criteria,
            source_refs=list(source_refs),
            model_version=self._version(item, "model_version", "openfang-learning"),
            prompt_version=self._version(item, "prompt_version", "rubric-prompt-v1"),
            source_version=self._version(item, "source_version", "course-revision"),
        )
        # Without an image the call is exactly the old one (boundaries without the keyword).
        decision = (
            self._rubric_evaluator.evaluate(request)
            if bild_png is None
            else self._rubric_evaluator.evaluate(request, bild_png=bild_png)
        )
        points = {value.criterion_id: value for value in criteria}
        shown: list[CriterionFeedback] = []
        # Unaccepted decisions go to human review; their per-criterion text is ungrounded.
        for result in (decision.criterion_results if decision.accepted else ()):
            known = points.get(result.criterion_id)
            if known is None:
                continue
            shown.append(
                CriterionFeedback(
                    criterion_id=known.criterion_id,
                    description=known.description,
                    score=min(1.0, max(0.0, result.awarded_points / known.points)),
                    feedback=result.feedback,
                )
            )
        return _Bewertung(
            response_id=response_id,
            evaluation_id=decision.evaluation_id,
            answer=answer,
            evaluator_type="rubric",
            score=decision.score,
            confidence=decision.confidence,
            accepted=decision.accepted,
            rationale_codes=tuple(decision.rationale_codes),
            evaluator_versions={
                **decision.evaluator_versions,
                "evidence": decision.evidence_ref,
            },
            source_refs=tuple(decision.source_refs),
            criteria=tuple(shown),
            review_reason=(
                None
                if decision.accepted
                else (decision.rationale_codes or [_FALLBACK_REVIEW_REASON])[0]
            ),
            review_details={
                "confidence": decision.confidence,
                "evidence_ref": decision.evidence_ref,
            },
        )

    def _uebernehmen(
        self,
        db: Session,
        row: LearningSession,
        attempt: LearningAttempt,
        item: AdaptiveItem,
        bewertung: _Bewertung,
        now: datetime,
    ) -> tuple[LearningEvaluation, _AnswerAudit]:
        """Persist a graded answer and apply mastery, misconceptions and the review queue."""
        encoded = json.dumps(bewertung.answer, sort_keys=True, separators=(",", ":"))
        response = LearningResponse(
            id=bewertung.response_id,
            attempt_id=attempt.id,
            response_revision=1,
            answer=bewertung.answer,
            answer_hash=hashlib.sha256(encoded.encode()).hexdigest(),
            created_at=now,
        )
        db.add(response)
        db.flush()
        evaluation = LearningEvaluation(
            id=bewertung.evaluation_id,
            response_id=response.id,
            evaluator_type=bewertung.evaluator_type,
            score=bewertung.score,
            confidence=bewertung.confidence,
            accepted=bewertung.accepted,
            rationale_codes=list(bewertung.rationale_codes),
            evaluator_versions=bewertung.evaluator_versions,
            source_refs=list(bewertung.source_refs),
            created_at=now,
        )
        db.add(evaluation)
        db.flush()
        successful = bewertung.score >= 0.65
        applied = False
        if bewertung.accepted:
            applied = self._apply_mastery_and_review(
                db,
                actor_id=row.actor_id,
                attempt=attempt,
                evaluation=evaluation,
                successful=successful,
                now=now,
            )
        else:
            assert bewertung.review_reason is not None
            db.add(
                LearningReviewItem(
                    id=str(uuid4()),
                    request_ref=f"session:{row.id}:{attempt.ordinal}",
                    evaluation_id=evaluation.id,
                    reason_code=bewertung.review_reason,
                    status="pending",
                    details=bewertung.review_details,
                    created_at=now,
                    resolved_at=None,
                )
            )
        self._update_misconceptions(
            db,
            actor_id=row.actor_id,
            item=item,
            evaluation=evaluation,
            successful=bewertung.accepted and successful,
        )
        return evaluation, _AnswerAudit(
            response.id, evaluation.id, applied, bewertung.criteria
        )

    def _apply_mastery_and_review(
        self,
        db: Session,
        *,
        actor_id: str,
        attempt: LearningAttempt,
        evaluation: LearningEvaluation,
        successful: bool,
        now: datetime,
    ) -> bool:
        concept_ids = tuple(
            sorted(
                db.scalars(
                    select(ItemConcept.concept_id).where(
                        ItemConcept.item_id == attempt.item_id,
                        ItemConcept.course_id == attempt.course_id,
                        ItemConcept.course_revision == attempt.course_revision,
                    )
                ).all()
            )
        )
        if not concept_ids:
            raise PersistenceConflict("evaluated item has no concepts")
        for concept_id in concept_ids:
            mastery = db.get(
                ConceptMastery,
                {"actor_id": actor_id, "concept_id": concept_id},
                with_for_update=True,
            )
            before = MasteryState(mastery.alpha, mastery.beta) if mastery else MasteryState()
            update = calculate_mastery_update(
                before,
                score=evaluation.score,
                confidence=evaluation.confidence,
                concept_count=len(concept_ids),
                accepted=evaluation.accepted,
            )
            if update is None:
                continue
            if mastery is None:
                mastery = ConceptMastery(
                    actor_id=actor_id,
                    concept_id=concept_id,
                    alpha=update.after.alpha,
                    beta=update.after.beta,
                    revision=2,
                    updated_at=now,
                )
                db.add(mastery)
            else:
                mastery.alpha = update.after.alpha
                mastery.beta = update.after.beta
                mastery.revision += 1
                mastery.updated_at = now
            db.add(
                MasteryEvidence(
                    id=str(uuid4()),
                    evaluation_id=evaluation.id,
                    concept_id=concept_id,
                    actor_id=actor_id,
                    applied_weight=update.weight,
                    applied_score=update.score,
                    alpha_before=update.before.alpha,
                    beta_before=update.before.beta,
                    alpha_after=update.after.alpha,
                    beta_after=update.after.beta,
                    created_at=now,
                )
            )
            review = db.get(
                ReviewSchedule,
                {"actor_id": actor_id, "concept_id": concept_id},
                with_for_update=True,
            )
            current = (
                ReviewState(review.interval_index, self._aware(review.due_at), review.maintenance)
                if review is not None
                else None
            )
            plan = schedule_review(current, successful=successful, now=now)
            if review is None:
                db.add(
                    ReviewSchedule(
                        actor_id=actor_id,
                        concept_id=concept_id,
                        interval_index=plan.interval_index,
                        due_at=plan.due_at,
                        maintenance=plan.maintenance,
                        alternate_representation_required=plan.alternate_representation_required,
                        last_evaluation_id=evaluation.id,
                        revision=1,
                        updated_at=now,
                    )
                )
            else:
                review.interval_index = plan.interval_index
                review.due_at = plan.due_at
                review.maintenance = plan.maintenance
                review.alternate_representation_required = plan.alternate_representation_required
                review.last_evaluation_id = evaluation.id
                review.revision += 1
                review.updated_at = now
        return True

    def _update_misconceptions(
        self,
        db: Session,
        *,
        actor_id: str,
        item: AdaptiveItem,
        evaluation: LearningEvaluation,
        successful: bool,
    ) -> None:
        tags = self._string_list(item.scoring_config.get("remediation_tags", []))
        concept_ids = db.scalars(
            select(ItemConcept.concept_id).where(ItemConcept.item_id == item.id)
        ).all()
        for concept_id in concept_ids:
            for tag in tags:
                row = db.scalar(
                    select(Misconception).where(
                        Misconception.actor_id == actor_id,
                        Misconception.concept_id == concept_id,
                        Misconception.tag == tag,
                    )
                )
                if successful:
                    if row is not None and row.status == "unresolved":
                        row.status = "resolved"
                        row.latest_evaluation_id = evaluation.id
                        row.resolved_at = self._aware_now()
                elif row is None:
                    db.add(
                        Misconception(
                            id=str(uuid4()),
                            actor_id=actor_id,
                            concept_id=concept_id,
                            tag=tag,
                            status="unresolved",
                            first_evaluation_id=evaluation.id,
                            latest_evaluation_id=evaluation.id,
                            created_at=self._aware_now(),
                            resolved_at=None,
                        )
                    )
                else:
                    row.status = "unresolved"
                    row.latest_evaluation_id = evaluation.id
                    row.resolved_at = None

    def _append_next(
        self, db: Session, row: LearningSession, items: tuple[AdaptiveItem, ...]
    ) -> LearningAttempt:
        ordinal = (
            db.scalar(
                select(func.max(LearningAttempt.ordinal)).where(
                    LearningAttempt.session_id == row.id
                )
            )
            or 0
        ) + 1
        item, reason = self._select_item(db, row, items, ordinal)
        attempt = LearningAttempt(
            id=str(uuid4()),
            session_id=row.id,
            item_id=item.id,
            course_id=row.course_id,
            course_revision=row.course_revision,
            ordinal=ordinal,
            selection_reason=reason,
            selected_at=self._aware_now(),
        )
        db.add(attempt)
        row.revision += 1
        row.updated_at = self._aware_now()
        db.flush()
        return attempt

    def _select_item(
        self,
        db: Session,
        row: LearningSession,
        items: tuple[AdaptiveItem, ...],
        ordinal: int,
    ) -> tuple[AdaptiveItem, dict[str, object]]:
        if row.mode == "exam":
            item_ids = row.blueprint.get("selected_item_ids")
            if not isinstance(item_ids, list) or ordinal > len(item_ids):
                raise PersistenceConflict("exam blueprint is exhausted")
            selected_id = item_ids[ordinal - 1]
            item = next((candidate for candidate in items if candidate.id == selected_id), None)
            if item is None:
                raise PersistenceConflict("exam blueprint item is unavailable")
            return item, {"mode": "exam_blueprint", "ordinal": ordinal}

        types = row.blueprint.get("item_types")
        if types:
            items = tuple(item for item in items if item.item_type in types)
        concepts_by_item = self._concepts_by_item(db, row.course_id, row.course_revision)
        concept_ids = sorted({value for values in concepts_by_item.values() for value in values})
        mastery_rows = db.scalars(
            select(ConceptMastery).where(
                ConceptMastery.actor_id == row.actor_id,
                ConceptMastery.concept_id.in_(concept_ids),
            )
        ).all()
        mastery = {concept_id: 0.5 for concept_id in concept_ids}
        mastery.update(
            {item.concept_id: item.alpha / (item.alpha + item.beta) for item in mastery_rows}
        )
        reviews = db.scalars(
            select(ReviewSchedule).where(
                ReviewSchedule.actor_id == row.actor_id,
                ReviewSchedule.concept_id.in_(concept_ids),
            )
        ).all()
        misconceptions: dict[str, list[str]] = {}
        for value in db.scalars(
            select(Misconception).where(
                Misconception.actor_id == row.actor_id,
                Misconception.concept_id.in_(concept_ids),
                Misconception.status == "unresolved",
            )
        ).all():
            misconceptions.setdefault(value.concept_id, []).append(value.tag)
        recent_attempts = db.scalars(
            select(LearningAttempt)
            .where(LearningAttempt.session_id == row.id)
            .order_by(LearningAttempt.ordinal.desc())
            .limit(10)
        ).all()
        item_by_id = {item.id: item for item in items}
        recent_representations: dict[str, str] = {}
        for attempt in recent_attempts:
            recent_item = item_by_id.get(attempt.item_id)
            if recent_item is None:
                continue
            representation = self._representation(recent_item)
            for concept_id in concepts_by_item.get(recent_item.id, ()):
                recent_representations.setdefault(concept_id, representation)
        context = SelectionContext(
            mastery=mastery,
            review_due_at={item.concept_id: self._aware(item.due_at) for item in reviews},
            unresolved_misconceptions={
                key: tuple(sorted(values)) for key, values in misconceptions.items()
            },
            recent_item_ids=tuple(item.item_id for item in recent_attempts),
            alternate_representation_concepts=frozenset(
                item.concept_id
                for item in reviews
                if item.alternate_representation_required
            ),
            recent_representations=recent_representations,
            now=self._aware_now(),
        )
        candidates = tuple(
            CandidateItem(
                item_id=item.id,
                difficulty=item.difficulty,
                concept_ids=concepts_by_item.get(item.id, ()),
                quality_status=item.quality_status,
                remediation_tags=self._string_list(
                    item.scoring_config.get("remediation_tags", [])
                ),
                representation=self._representation(item),
            )
            for item in items
        )
        # A task solved fully correctly in this session is not asked again while other
        # tasks remain (live 24.10.: the same correct open task came twice).
        solved = self._solved_item_ids(db, row.id)
        open_candidates = tuple(c for c in candidates if c.item_id not in solved) or candidates
        result = select_next_item(open_candidates, context)
        if result.selected is None and context.alternate_representation_concepts:
            # Only one representation exists for the concept (e.g. all single choice):
            # the alternate-representation rule must not end the session.
            result = select_next_item(
                open_candidates, replace(context, alternate_representation_concepts=frozenset())
            )
        if result.selected is None:
            raise LookupError("no eligible adaptive item is available")
        selected = item_by_id[result.selected.item_id]
        reason = asdict(result.selected)
        reason["reason_codes"] = list(result.selected.reason_codes)
        return selected, reason

    @staticmethod
    def _solved_item_ids(db: Session, session_id: str) -> set[str]:
        return set(
            db.scalars(
                select(LearningAttempt.item_id)
                .join(LearningResponse, LearningResponse.attempt_id == LearningAttempt.id)
                .join(LearningEvaluation, LearningEvaluation.response_id == LearningResponse.id)
                .where(LearningAttempt.session_id == session_id, LearningEvaluation.score >= _SOLVED_SCORE)
            ).all()
        )

    def _turn(
        self,
        db: Session,
        row: LearningSession,
        *,
        feedback: TurnFeedback | None = None,
        answer_audit: _AnswerAudit | None = None,
    ) -> SessionTurn:
        task = None
        if row.state == "active":
            attempt = self._current_attempt(db, row.id)
            item = db.get(AdaptiveItem, attempt.item_id) if attempt else None
            task = self._public_task(item, row.id) if item is not None else None
        summary = self._summary(db, row) if row.state == "completed" else None
        return SessionTurn(
            session_id=row.id,
            course_id=row.course_id,
            session_revision=row.revision,
            state=row.state,
            mode=row.mode,
            task=task,
            progress=self._progress(db, row),
            feedback=feedback,
            summary=summary,
            audit=AuditReceipt(
                response_id=answer_audit.response_id if answer_audit else None,
                evaluation_id=answer_audit.evaluation_id if answer_audit else None,
                mastery_applied=answer_audit.mastery_applied if answer_audit else False,
                readback_verified=False,
            ),
        )

    def _summary(self, db: Session, row: LearningSession) -> SessionSummary:
        values = db.execute(
            select(LearningAttempt.ordinal, LearningAttempt.item_id, LearningEvaluation.score,
                   LearningEvaluation.evaluator_type)
            .join(LearningResponse, LearningResponse.attempt_id == LearningAttempt.id)
            .join(LearningEvaluation, LearningEvaluation.response_id == LearningResponse.id)
            .where(LearningAttempt.session_id == row.id)
            .order_by(LearningAttempt.ordinal)
        ).all()
        # A person's review ("manual") replaces the AI evaluation it decided on.
        effective: dict[int, tuple[str, float, bool]] = {}
        for ordinal, item_id, value, evaluator in values:
            manual = evaluator == "manual"
            if ordinal not in effective or (manual and not effective[ordinal][2]):
                effective[ordinal] = (item_id, value, manual)
        results = tuple(
            ExamResult(ordinal=ordinal, item_id=item_id, score=value)
            for ordinal, (item_id, value, _) in sorted(effective.items())
        )
        score = sum(item.score for item in results) / len(results) if results else 0
        return SessionSummary(
            attempted=len(results),
            correct=sum(item.score >= 0.65 for item in results),
            score=score,
            results=results,
        )

    def _progress(self, db: Session, row: LearningSession) -> SessionProgress:
        completed = db.scalar(
            select(func.count(LearningResponse.id))
            .join(LearningAttempt, LearningAttempt.id == LearningResponse.attempt_id)
            .where(LearningAttempt.session_id == row.id)
        ) or 0
        return SessionProgress(completed=completed, total=self._total(row))

    def _with_readback(self, turn: SessionTurn) -> SessionTurn:
        with self._session_factory() as db:
            row = db.get(LearningSession, turn.session_id)
            verified = row is not None and row.revision == turn.session_revision
            if turn.audit.evaluation_id:
                evaluation = db.get(LearningEvaluation, turn.audit.evaluation_id)
                evidence_count = db.scalar(
                    select(func.count(MasteryEvidence.id)).where(
                        MasteryEvidence.evaluation_id == turn.audit.evaluation_id
                    )
                )
                review_count = db.scalar(
                    select(func.count(LearningReviewItem.id)).where(
                        LearningReviewItem.evaluation_id == turn.audit.evaluation_id
                    )
                )
                verified = (
                    verified
                    and evaluation is not None
                    and bool(evidence_count or review_count)
                )
        if not verified:
            raise PersistenceConflict("adaptive session readback failed")
        return turn.model_copy(
            update={"audit": turn.audit.model_copy(update={"readback_verified": True})}
        )

    def _normalize_blueprint(
        self, command: SessionStartCommand, items: tuple[AdaptiveItem, ...]
    ) -> dict[str, object]:
        if command.mode == "training":
            maximum = command.blueprint.get("max_attempts", 20)
            if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 500:
                raise ValueError("training max_attempts must be between 1 and 500")
            types = command.blueprint.get("item_types")
            if types is None:
                return {"max_attempts": maximum}
            # Optional: practise only some task types (e.g. only drawing tasks).
            if not isinstance(types, list) or not types or any(
                not isinstance(value, str) for value in types
            ):
                raise ValueError("training item_types are invalid")
            if not any(item.item_type in types for item in items):
                raise LookupError("the course has no task of the requested types")
            return {"max_attempts": maximum, "item_types": list(types)}
        count = command.blueprint.get("item_count")
        types = command.blueprint.get("item_types", [])
        bands = command.blueprint.get("difficulty_bands", [])
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 200:
            raise ValueError("exam item_count is invalid")
        if not isinstance(types, list) or any(not isinstance(value, str) for value in types):
            raise ValueError("exam item_types are invalid")
        if not isinstance(bands, list):
            raise ValueError("exam difficulty bands are invalid")
        eligible = [item for item in items if not types or item.item_type in types]
        selected: list[AdaptiveItem] = []
        for band in bands:
            if (
                not isinstance(band, list)
                or len(band) != 2
                or any(isinstance(value, bool) or not isinstance(value, int | float) for value in band)
            ):
                raise ValueError("exam difficulty band is invalid")
            low, high = float(band[0]), float(band[1])
            candidates = [item for item in eligible if low <= item.difficulty <= high and item not in selected]
            if candidates:
                midpoint = (low + high) / 2
                selected.append(min(candidates, key=lambda item: (abs(item.difficulty - midpoint), item.id)))
            if len(selected) == count:
                break
        for item in sorted(eligible, key=lambda value: (value.difficulty, value.id)):
            if item not in selected and len(selected) < count:
                selected.append(item)
        if len(selected) != count:
            raise LookupError("exam blueprint cannot be satisfied")
        return {
            "item_count": count,
            "item_types": list(types),
            "difficulty_bands": bands,
            "selected_item_ids": [item.id for item in selected],
        }

    def _catalog(self, db: Session, course_id: str, revision: int) -> tuple[AdaptiveItem, ...]:
        concept = db.scalar(
            select(AdaptiveConcept.id).where(
                AdaptiveConcept.course_id == course_id,
                AdaptiveConcept.course_revision == revision,
            ).limit(1)
        )
        if concept is None:
            raise LookupError("adaptive course revision has no concepts")
        admitted_types = set(_DETERMINISTIC_TYPES)
        if self._rubric_evaluator is not None:
            admitted_types.update(_RUBRIC_TYPES)
            admitted_types.add("penecho_canvas")
        admitted_types.update(self._external_rubric_types)
        return tuple(
            db.scalars(
                select(AdaptiveItem)
                .where(
                    AdaptiveItem.course_id == course_id,
                    AdaptiveItem.course_revision == revision,
                    AdaptiveItem.quality_status == "approved",
                    AdaptiveItem.item_type.in_(admitted_types),
                )
                .order_by(AdaptiveItem.id)
            ).all()
        )

    @staticmethod
    def _version(item: AdaptiveItem, key: str, default: str) -> str:
        value = item.scoring_config.get(key, default)
        if not isinstance(value, str) or not value or len(value) > 200:
            raise ValueError(f"rubric item {key} is invalid")
        return value

    @staticmethod
    def _concepts_by_item(db: Session, course_id: str, revision: int) -> dict[str, tuple[str, ...]]:
        values: dict[str, list[str]] = {}
        for item_id, concept_id in db.execute(
            select(ItemConcept.item_id, ItemConcept.concept_id).where(
                ItemConcept.course_id == course_id,
                ItemConcept.course_revision == revision,
            )
        ):
            values.setdefault(item_id, []).append(concept_id)
        return {key: tuple(sorted(value)) for key, value in values.items()}

    @staticmethod
    def _current_attempt(db: Session, session_id: str) -> LearningAttempt | None:
        return db.scalar(
            select(LearningAttempt)
            .where(LearningAttempt.session_id == session_id)
            .order_by(LearningAttempt.ordinal.desc())
            .limit(1)
        )

    @classmethod
    def _public_task(cls, item: AdaptiveItem, session_id: str) -> PublicTask:
        # Stored orders of old rows were recomputable from public ids and the table is
        # append-only, so the shown order is decided here: keyed, stable within a session.
        salz = f"{session_id}:{item.id}"
        expected = item.expected_answer if isinstance(item.expected_answer, dict) else {}
        order = expected.get("order") if item.item_type == "ordering" else None
        options = zufall.verdeckt_sortiert(
            list(cls._options(item)), salz, lambda o: o.id, order if isinstance(order, list) else None,
        )
        presentation = cls._presentation(item)
        right = presentation.get("right")
        if item.item_type == "matching" and isinstance(right, list):
            pairs = expected.get("pairs")
            left = presentation.get("left")
            aligned = None
            if isinstance(pairs, dict) and isinstance(left, list):
                aligned = [pairs.get(e.get("id")) for e in left if isinstance(e, dict)]
            presentation = {**presentation, "right": zufall.verdeckt_sortiert(
                right, salz, lambda r: r.get("id") if isinstance(r, dict) else r, aligned,
            )}
        return PublicTask(
            id=item.id,
            type=item.item_type,
            prompt=cls._string_config(item, "prompt"),
            options=tuple(options),
            difficulty=item.difficulty,
            representation=cls._representation(item),
            source_refs=cls._anzeige_refs(item),
            presentation=presentation,
        )

    @classmethod
    def _anzeige_refs(cls, item: AdaptiveItem) -> tuple[str, ...]:
        """What the learner is shown: the filtered refs when the item has them, else all.

        Grading reads `source_refs` directly and always sees every source."""
        config = item.scoring_config
        return cls._string_list(config["anzeige_refs"] if "anzeige_refs" in config else config.get("source_refs", []))

    @staticmethod
    def _options(item: AdaptiveItem) -> tuple[ChoiceOption, ...]:
        raw = item.scoring_config.get("options", [])
        if not isinstance(raw, list):
            raise ValueError("adaptive item options are invalid")
        return tuple(ChoiceOption.model_validate(value) for value in raw)

    @staticmethod
    def _string_config(item: AdaptiveItem, key: str) -> str:
        value = item.scoring_config.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"adaptive item {key} is missing")
        return value

    @staticmethod
    def _string_list(value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("adaptive item string list is invalid")
        return tuple(value)

    @staticmethod
    def _representation(item: AdaptiveItem) -> str:
        value = item.scoring_config.get("representation", item.item_type)
        if not isinstance(value, str) or not value:
            raise ValueError("adaptive item representation is invalid")
        return value

    @staticmethod
    def _presentation(item: AdaptiveItem) -> dict[str, object]:
        allowed_by_type = {
            # Both sides of a matching task; the solution (pairs) never travels.
            "matching": frozenset({"left", "right"}),
            # The command with its blank, and which shell it is for; or the drawn window
            # with the position of its box (and of the boxes that repeat it).
            "fill_in": frozenset({"template", "shell", "image", "width", "height", "rect", "more"}),
            "numeric": frozenset({"unit"}),
            "penecho_canvas": frozenset({"canvas_href"}),
        }
        # `math`: the Werkstatt switches formula typesetting on for math courses, any type.
        allowed = allowed_by_type.get(item.item_type, frozenset()) | {"math"}
        raw = item.scoring_config.get("presentation", {})
        if not isinstance(raw, dict):
            raise ValueError("adaptive item presentation is invalid")
        return {key: value for key, value in raw.items() if key in allowed}

    @staticmethod
    def _feedback(
        evaluation: LearningEvaluation,
        criteria: tuple[CriterionFeedback, ...],
    ) -> TurnFeedback:
        return _turn_feedback(
            accepted=evaluation.accepted,
            score=evaluation.score,
            rationale_codes=tuple(evaluation.rationale_codes),
            source_refs=tuple(evaluation.source_refs),
            criteria=criteria,
        )

    @staticmethod
    def _feedback_aus(bewertung: _Bewertung) -> TurnFeedback:
        """The feedback a preview shows: the same bands as a booked answer."""
        return _turn_feedback(
            accepted=bewertung.accepted,
            score=bewertung.score,
            rationale_codes=bewertung.rationale_codes,
            source_refs=bewertung.source_refs,
            criteria=bewertung.criteria,
        )

    @staticmethod
    def _total(row: LearningSession) -> int:
        key = "max_attempts" if row.mode == "training" else "item_count"
        value = row.blueprint.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise PersistenceConflict("adaptive session blueprint is invalid")
        return value

    @staticmethod
    def _assert_owned(row: LearningSession | None, actor_id: str) -> None:
        if row is None:
            raise LookupError("adaptive session not found")
        if row.actor_id != actor_id:
            raise PermissionError("adaptive session belongs to another actor")

    def _assert_owned_active(self, row: LearningSession | None, actor_id: str) -> None:
        self._assert_owned(row, actor_id)
        assert row is not None
        if row.state != "active":
            raise PersistenceConflict("adaptive session is not active")

    def _aware_now(self) -> datetime:
        return self._aware(self._clock())

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _turn_feedback(
    *,
    accepted: bool,
    score: float,
    rationale_codes: tuple[str, ...],
    source_refs: tuple[str, ...],
    criteria: tuple[CriterionFeedback, ...],
) -> TurnFeedback:
    state = (
        "needs_review"
        if not accepted
        else "correct"
        if score >= 0.999
        else "partial"
        if score >= 0.65
        else "incorrect"
    )
    return TurnFeedback(
        state=state,
        score=score,
        rationale_codes=rationale_codes,
        source_refs=source_refs,
        criteria=criteria,
    )


def _review_entry(review, evaluation, response, item, session_row) -> dict[str, object]:
    answer = response.answer if isinstance(response.answer, dict) else {}
    snapshot = answer.get("rendered_snapshot") if answer.get("kind") == "penecho_canvas" else None
    expected = item.expected_answer if isinstance(item.expected_answer, dict) else {}
    text = answer.get("text")
    return {
        "review_id": review.id,
        "reason": review.reason_code,
        "created_at": review.created_at.isoformat(),
        "course_id": session_row.course_id,
        "session_id": session_row.id,
        "actor_id": session_row.actor_id,
        "item_type": item.item_type,
        "task_prompt": item.scoring_config.get("prompt"),
        "expected_answer": expected.get("text"),
        "rubric": list(item.rubric or []),
        "answer_text": text if isinstance(text, str) else None,
        "drawing_locator": snapshot.get("locator") if isinstance(snapshot, dict) else None,
        "ai_score": evaluation.score,
        "ai_confidence": evaluation.confidence,
    }
