from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from spaces.learning.services.course_factory.model_gateway import (
    ModelGateway,
    ModelImageInput,
    png_bild,
)
from spaces.learning.services.evaluation.penecho import _ImageBoundGateway
from spaces.learning.services.evaluation.review_queue import ReviewItem
from spaces.learning.services.evaluation.rubric import RubricEvaluator
from spaces.learning.services.evaluation.schemas import (
    RubricDecision,
    RubricEvaluationRequest,
)


@dataclass
class _DecisionRecorder:
    decision: RubricDecision | None = None

    def record(self, value: RubricDecision) -> None:
        self.decision = value


@dataclass
class _ReviewCapture:
    items: list[ReviewItem] = field(default_factory=list)

    def enqueue(self, item: ReviewItem) -> None:
        self.items.append(item)


class _DeferredMastery:
    def apply(self, value: RubricDecision) -> None:
        del value


class SessionRubricEvaluator:
    """Runs the validated rubric boundary while deferring persistence to the session UoW."""

    def __init__(self, gateway_factory: Callable[[], ModelGateway]) -> None:
        # One gateway per call: each call runs on its own event loop
        # (asyncio.run), and a pooled HTTP client must not outlive its loop.
        self._gateway_factory = gateway_factory

    def evaluate(
        self,
        request: RubricEvaluationRequest,
        bild: ModelImageInput | None = None,
        *,
        bild_png: bytes | None = None,
    ) -> RubricDecision:
        # `bild_png` is the session-service contract (raw PNG); `bild` stays for the
        # evaluation API, which validates the image itself.
        if bild is not None and bild_png is not None:
            raise ValueError("rubric evaluation takes one image")
        if bild_png is not None:
            bild = png_bild(bild_png)
        recorder = _DecisionRecorder()
        decision = asyncio.run(self._evaluate(request, recorder, bild))
        if recorder.decision != decision:
            raise RuntimeError("rubric decision capture failed")
        return decision

    async def _evaluate(
        self,
        request: RubricEvaluationRequest,
        recorder: _DecisionRecorder,
        bild: ModelImageInput | None = None,
    ) -> RubricDecision:
        gateway = self._gateway_factory()
        try:
            return await RubricEvaluator(
                gateway=gateway if bild is None else _ImageBoundGateway(gateway, bild),
                recorder=recorder,
                review_queue=_ReviewCapture(),
                mastery=_DeferredMastery(),
            ).evaluate(request)
        finally:
            close = getattr(gateway, "aclose", None)
            if close is not None:
                await close()
