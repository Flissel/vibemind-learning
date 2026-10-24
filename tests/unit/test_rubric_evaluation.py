from __future__ import annotations

from uuid import uuid4

import pytest

from spaces.learning.services.course_factory.model_gateway import (
    GatewayResult,
    GatewaySchemaError,
    GatewayUnavailable,
)
from spaces.learning.services.evaluation.review_queue import InMemoryReviewQueue
from spaces.learning.services.evaluation.rubric import (
    RubricEvaluationUnavailable,
    RubricEvaluator,
)
from spaces.learning.services.evaluation.schemas import (
    CriterionEvaluation,
    RubricCriterion,
    RubricEvaluationRequest,
    RubricModelOutput,
)


def _request() -> RubricEvaluationRequest:
    return RubricEvaluationRequest(
        evaluation_id=str(uuid4()),
        response_id=str(uuid4()),
        response={"text": "Provider execution stays behind OpenFang."},
        expected_answer="Explain the authorized provider boundary.",
        rubric=[
            RubricCriterion(
                criterion_id="authority",
                description="Identifies the authority boundary",
                points=2,
            ),
            RubricCriterion(
                criterion_id="readback",
                description="Requires terminal readback",
                points=1,
            ),
        ],
        source_refs=["source://authority/3", "source://readback/2"],
        model_version="openfang-learning-v1",
        prompt_version="rubric-v1",
        source_version="course-sources-r3",
    )


def _output(confidence: float = 0.9) -> RubricModelOutput:
    return RubricModelOutput(
        schema_version="rubric-evaluation-v1",
        criteria=[
            CriterionEvaluation(
                criterion_id="authority",
                awarded_points=2,
                feedback="The authority boundary is correct.",
                source_refs=["source://authority/3"],
            ),
            CriterionEvaluation(
                criterion_id="readback",
                awarded_points=0.5,
                feedback="The terminal readback is only implicit.",
                source_refs=["source://readback/2"],
            ),
        ],
        confidence=confidence,
        misconception_tags=["readback_implicit"],
    )


class _Gateway:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def generate(self, invocation, output_model):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert invocation.role == "rubric_evaluator"
        assert invocation.output_schema_version == "rubric-evaluation-v1"
        assert isinstance(outcome, output_model)
        return GatewayResult(
            output=outcome,
            evidence_ref=f"openfang://completion/rubric-{self.calls}",
        )


class _Recorder:
    def __init__(self) -> None:
        self.values = []

    def record(self, value) -> None:
        self.values.append(value)


class _Mastery:
    def __init__(self) -> None:
        self.values = []

    def apply(self, value) -> None:
        self.values.append(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confidence", "accepted", "review_count", "mastery_count"),
    [(0.649, False, 1, 0), (0.65, True, 0, 1)],
)
async def test_confidence_boundary_gates_mastery(
    confidence, accepted, review_count, mastery_count
) -> None:
    recorder = _Recorder()
    mastery = _Mastery()
    review = InMemoryReviewQueue()
    evaluator = RubricEvaluator(
        gateway=_Gateway([_output(confidence)]),
        recorder=recorder,
        review_queue=review,
        mastery=mastery,
    )

    decision = await evaluator.evaluate(_request())

    assert decision.score == pytest.approx(2.5 / 3)
    assert decision.confidence == confidence
    assert decision.accepted is accepted
    assert decision.evaluator_versions == {
        "model": "openfang-learning-v1",
        "prompt": "rubric-v1",
        "sources": "course-sources-r3",
    }
    assert len(recorder.values) == 1
    assert len(review.items) == review_count
    assert len(mastery.values) == mastery_count


@pytest.mark.asyncio
async def test_feedback_must_be_grounded_in_declared_sources() -> None:
    original = _output()
    output = original.model_copy(
        update={
            "criteria": [
                original.criteria[0].model_copy(
                    update={"source_refs": ["source://invented/99"]}
                ),
                original.criteria[1],
            ]
        }
    )
    review = InMemoryReviewQueue()
    mastery = _Mastery()
    evaluator = RubricEvaluator(
        gateway=_Gateway([output]),
        recorder=_Recorder(),
        review_queue=review,
        mastery=mastery,
    )

    decision = await evaluator.evaluate(_request())

    assert decision.accepted is False
    assert decision.rationale_codes == ["unsupported_feedback"]
    assert len(review.items) == 1
    assert mastery.values == []


@pytest.mark.asyncio
async def test_transient_gateway_failure_retries_once() -> None:
    gateway = _Gateway([GatewayUnavailable("offline"), _output()])
    evaluator = RubricEvaluator(
        gateway=gateway,
        recorder=_Recorder(),
        review_queue=InMemoryReviewQueue(),
        mastery=_Mastery(),
        max_attempts=2,
    )

    decision = await evaluator.evaluate(_request())

    assert decision.accepted is True
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_schema_failure_creates_review_without_mastery() -> None:
    review = InMemoryReviewQueue()
    mastery = _Mastery()
    evaluator = RubricEvaluator(
        gateway=_Gateway([GatewaySchemaError("invalid")]),
        recorder=_Recorder(),
        review_queue=review,
        mastery=mastery,
    )

    with pytest.raises(RubricEvaluationUnavailable, match="schema"):
        await evaluator.evaluate(_request())

    assert review.items[0].reason_code == "evaluator_schema_invalid"
    assert mastery.values == []


@pytest.mark.asyncio
async def test_criterion_contract_must_match_request_exactly() -> None:
    original = _output()
    output = original.model_copy(update={"criteria": original.criteria[:1]})
    review = InMemoryReviewQueue()
    mastery = _Mastery()
    evaluator = RubricEvaluator(
        gateway=_Gateway([output]),
        recorder=_Recorder(),
        review_queue=review,
        mastery=mastery,
    )

    decision = await evaluator.evaluate(_request())

    assert decision.accepted is False
    assert decision.rationale_codes == ["rubric_contract_mismatch"]
    assert mastery.values == []


@pytest.mark.asyncio
async def test_model_cites_short_source_keys_mapped_back_to_full_refs() -> None:
    # Live, refs were long JSON strings with the chunk content; the model did not
    # echo them verbatim and every correct answer landed in review
    # ("unsupported_feedback").
    seen = {}

    class _KeyGateway(_Gateway):
        async def generate(self, invocation, output_model):
            seen["sources"] = invocation.input_payload["sources"]
            return await super().generate(invocation, output_model)

    keyed = _output().model_copy(
        update={
            "criteria": [
                _output().criteria[0].model_copy(update={"source_refs": ["S1"]}),
                _output().criteria[1].model_copy(update={"source_refs": ["S2"]}),
            ]
        }
    )
    mastery = _Mastery()
    evaluator = RubricEvaluator(
        gateway=_KeyGateway([keyed]),
        recorder=_Recorder(),
        review_queue=InMemoryReviewQueue(),
        mastery=mastery,
    )

    decision = await evaluator.evaluate(_request())

    assert seen["sources"] == [
        {"key": "S1", "source": "source://authority/3"},
        {"key": "S2", "source": "source://readback/2"},
    ]
    assert decision.accepted is True and len(mastery.values) == 1
    assert [c.source_refs for c in decision.criterion_results] == [
        ["source://authority/3"],
        ["source://readback/2"],
    ]


def test_session_evaluator_builds_a_fresh_gateway_per_call() -> None:
    # Each call runs on its own event loop (asyncio.run); a shared OpenFang
    # client kept connections of a closed loop - live "Event loop is closed".
    from spaces.learning.services.evaluation.session_rubric import SessionRubricEvaluator

    built = []

    def factory():
        gateway = _Gateway([_output()])
        built.append(gateway)
        return gateway

    evaluator = SessionRubricEvaluator(factory)
    evaluator.evaluate(_request())
    evaluator.evaluate(_request())

    assert len(built) == 2
    assert [gateway.calls for gateway in built] == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("points", "outcome"),
    [((2, 1), "answer_correct"), ((2, 0), "answer_partial"), ((0, 0), "answer_incorrect")],
)
async def test_accepted_rubric_decision_names_the_outcome(points, outcome) -> None:
    # Live (24.10.): a drawing scored 0 carried only "rubric_accepted", which reads
    # like a pass. The first code still says the decision is trustworthy.
    output = _output().model_copy(
        update={
            "criteria": [
                c.model_copy(update={"awarded_points": p})
                for c, p in zip(_output().criteria, points)
            ]
        }
    )
    recorder = _Recorder()
    evaluator = RubricEvaluator(
        gateway=_Gateway([output]),
        recorder=recorder,
        review_queue=InMemoryReviewQueue(),
        mastery=_Mastery(),
    )

    decision = await evaluator.evaluate(_request())

    assert decision.rationale_codes == ["rubric_accepted", outcome]
