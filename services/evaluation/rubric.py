from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from spaces.learning.services.course_factory.model_gateway import (
    ROLE_CONTRACTS,
    GatewaySchemaError,
    GatewayUnavailable,
    ModelGateway,
    ModelInvocation,
)
from spaces.learning.services.evaluation.review_queue import ReviewItem, ReviewQueue
from spaces.learning.services.evaluation.schemas import (
    RubricCriterion,
    RubricDecision,
    RubricEvaluationRequest,
    RubricModelOutput,
)


_CONFIDENCE_THRESHOLD = 0.65


class RubricEvaluationUnavailable(RuntimeError):
    pass


def rubric_contract_violation(
    awarded: Sequence[tuple[str, float]], rubric: Sequence[RubricCriterion]
) -> str | None:
    """The one rubric contract check, shared by the model path and external graders.

    `awarded` is (criterion_id, awarded_points) per graded criterion. The grade must
    name every rubric criterion exactly once, no other, with 0 <= points <= maximum.
    Returns the first violation as a short reason, or None when the contract holds.
    """
    maximum = {item.criterion_id: item.points for item in rubric}
    ids = [criterion_id for criterion_id, _ in awarded]
    if len(ids) != len(set(ids)):
        return "duplicate criterion"
    if set(ids) != set(maximum):
        return "criteria do not match the rubric"
    for criterion_id, points in awarded:
        if points < 0:
            return f"negative points for {criterion_id}"
        if points > maximum[criterion_id]:
            return f"points above the maximum for {criterion_id}"
    return None


def rubric_score(awarded_points: Sequence[float], rubric: Sequence[RubricCriterion]) -> float:
    """Sum of awarded points over the sum of maximum points; call after the contract check."""
    return sum(awarded_points) / sum(item.points for item in rubric)


def answer_outcome_code(score: float) -> str:
    """Names the outcome of an accepted rubric decision (same bands as the session feedback)."""
    return (
        "answer_correct" if score >= 0.999
        else "answer_partial" if score >= 0.65
        else "answer_incorrect"
    )


class EvaluationRecorder(Protocol):
    def record(self, value: RubricDecision) -> None: ...


class MasteryApplication(Protocol):
    def apply(self, value: RubricDecision) -> None: ...


class RubricEvaluator:
    CONFIDENCE_THRESHOLD = _CONFIDENCE_THRESHOLD

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        recorder: EvaluationRecorder,
        review_queue: ReviewQueue,
        mastery: MasteryApplication,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1 or max_attempts > 3:
            raise ValueError("rubric evaluator attempts must be between one and three")
        self._gateway = gateway
        self._recorder = recorder
        self._review_queue = review_queue
        self._mastery = mastery
        self._max_attempts = max_attempts

    async def evaluate(self, request: RubricEvaluationRequest) -> RubricDecision:
        UUID(request.evaluation_id)
        # Source refs can be long canonical JSON with the chunk text; a model does
        # not echo those verbatim. It cites short keys, mapped back below.
        keys = {f"S{index}": ref for index, ref in enumerate(request.source_refs, start=1)}
        invocation = ModelInvocation(
            role="rubric_evaluator",
            stage="evaluation",
            correlation_id=request.evaluation_id,
            prompt_version=ROLE_CONTRACTS["rubric_evaluator"][1],
            output_schema_version="rubric-evaluation-v1",
            input_payload={
                "response": request.response,
                "expected_answer": request.expected_answer,
                "rubric": [item.model_dump(mode="json") for item in request.rubric],
                "sources": [{"key": key, "source": ref} for key, ref in keys.items()],
                "declared_versions": {
                    "model": request.model_version,
                    "prompt": request.prompt_version,
                    "sources": request.source_version,
                },
            },
        )
        result = None
        for attempt in range(self._max_attempts):
            try:
                result = await self._gateway.generate(invocation, RubricModelOutput)
                break
            except GatewayUnavailable as error:
                if attempt + 1 == self._max_attempts:
                    self._enqueue_failure(request, "evaluator_unavailable")
                    raise RubricEvaluationUnavailable(
                        "rubric evaluator is unavailable"
                    ) from error
            except GatewaySchemaError as error:
                self._enqueue_failure(request, "evaluator_schema_invalid")
                raise RubricEvaluationUnavailable(
                    "rubric evaluator returned schema-invalid output"
                ) from error
        if result is None:
            raise RubricEvaluationUnavailable("rubric evaluator produced no result")

        output = result.output.model_copy(
            update={
                "criteria": [
                    item.model_copy(
                        update={"source_refs": [keys.get(ref, ref) for ref in item.source_refs]}
                    )
                    for item in result.output.criteria
                ]
            }
        )
        contract_matches = (
            rubric_contract_violation(
                [(item.criterion_id, item.awarded_points) for item in output.criteria],
                request.rubric,
            )
            is None
        )
        allowed_sources = set(request.source_refs)
        feedback_grounded = all(
            item.source_refs and set(item.source_refs) <= allowed_sources
            for item in output.criteria
        )
        if contract_matches:
            score = rubric_score(
                [item.awarded_points for item in output.criteria], request.rubric
            )
        else:
            score = 0.0

        if not contract_matches:
            rationale = "rubric_contract_mismatch"
        elif not feedback_grounded:
            rationale = "unsupported_feedback"
        elif output.confidence < _CONFIDENCE_THRESHOLD:
            rationale = "low_confidence"
        else:
            rationale = "rubric_accepted"
        accepted = rationale == "rubric_accepted"
        rationale_codes = [rationale]
        if accepted:
            # "rubric_accepted" says the decision is trustworthy, not that the answer
            # is right; name the outcome too (same bands as the session feedback).
            rationale_codes.append(answer_outcome_code(score))
        decision = RubricDecision(
            evaluation_id=request.evaluation_id,
            response_id=request.response_id,
            score=score,
            confidence=output.confidence,
            accepted=accepted,
            rationale_codes=rationale_codes,
            criterion_results=output.criteria,
            misconception_tags=output.misconception_tags,
            evaluator_versions={
                "model": request.model_version,
                "prompt": request.prompt_version,
                "sources": request.source_version,
            },
            source_refs=request.source_refs,
            evidence_ref=result.evidence_ref,
        )
        self._recorder.record(decision)
        if accepted:
            self._mastery.apply(decision)
        else:
            self._review_queue.enqueue(
                ReviewItem(
                    evaluation_id=request.evaluation_id,
                    reason_code=rationale,
                    details={
                        "confidence": output.confidence,
                        "evidence_ref": result.evidence_ref,
                    },
                )
            )
        return decision

    def _enqueue_failure(
        self, request: RubricEvaluationRequest, reason_code: str
    ) -> None:
        self._review_queue.enqueue(
            ReviewItem(
                evaluation_id=request.evaluation_id,
                reason_code=reason_code,
                details={
                    "model_version": request.model_version,
                    "prompt_version": request.prompt_version,
                    "source_version": request.source_version,
                },
            )
        )
