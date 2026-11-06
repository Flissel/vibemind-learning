from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveItem,
    LearningEvaluation,
    LearningResponse,
    MasteryEvidence,
)
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
)
from spaces.learning.services.db.models import Base

NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def _zeilen(factory) -> tuple[int, int, int]:
    with factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model)) or 0
            for model in (LearningResponse, LearningEvaluation, MasteryEvidence)
        )


def test_bewerten_schreibt_nichts(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'bewerten.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    item = AdaptiveItem(
        id=str(uuid4()),
        course_id=str(uuid4()),
        course_revision=1,
        activity_id="zahl",
        item_type="numeric",
        difficulty=0.5,
        quality_status="approved",
        expected_answer={"value": 2},
        scoring_config={"prompt": "Wie viel ist 1 + 1?", "source_refs": ["source://z/1"]},
        rubric=[],
    )
    service = AdaptiveSessionService(factory, clock=lambda: NOW)
    vorher = _zeilen(factory)

    bewertung = service._bewerten(item, {"value": 2})

    assert _zeilen(factory) == vorher
    assert bewertung.evaluator_type == "deterministic"
    assert bewertung.score == 1.0
    assert bewertung.response_id != bewertung.evaluation_id
    engine.dispose()
