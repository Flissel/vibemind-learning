"""Build or load learning sequences and print their embed link.

    python -m spaces.learning.services.lernsequenz.cli praesentation --course <course_id> [--chapter <id>]
    python -m spaces.learning.services.lernsequenz.cli laden <sequence.json>

Runs inside the learning-api container (database, artifact root and service key from its
environment). The link is for the LearnHouse embed block; it carries the sequence token.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy import select

from spaces.learning.contracts.lernsequenz import LernsequenzV1
from spaces.learning.deployment.lernsequenz_routes import sequence_token
from spaces.learning.services.course_factory.artifact_store import CourseFactoryArtifactStore
from spaces.learning.services.course_factory.models import CourseFactoryJob
from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.db.session import build_session_factory, create_learning_engine
from spaces.learning.services.lernsequenz.aus_kurs import build_presentation
from spaces.learning.services.lernsequenz.store import SequenceStore

PUBLIC_BASE_ENV = "LEARNING_PUBLIC_API_URL"


def _link(sequence_id: str) -> str:
    base = os.environ.get(PUBLIC_BASE_ENV, "http://127.0.0.1:8090").rstrip("/")
    return f"{base}/lernsequenz/{sequence_id}?k={sequence_token(sequence_id)}"


def published_draft(course_id: str) -> CourseDraft:
    factory = build_session_factory(create_learning_engine(os.environ["LEARNING_DATABASE_URL"]))
    with factory() as session:
        job = session.scalars(
            select(CourseFactoryJob)
            .where(CourseFactoryJob.course_id == course_id, CourseFactoryJob.state == FactoryState.PUBLISHED.value)
            .order_by(CourseFactoryJob.updated_at.desc())
        ).first()
        if job is None:
            raise LookupError("no published course factory job for this course")
        job_id, attempt = job.id, job.current_attempt
    store = CourseFactoryArtifactStore(factory, artifact_root=Path(os.environ["LEARNING_ARTIFACT_ROOT"]))
    payload = store.read_stage_output(job_id, attempt_number=attempt, stage=FactoryState.VERIFYING)
    verifier = payload.get("source_verifier")
    if not isinstance(verifier, dict):
        raise RuntimeError("verified factory artifact is missing")
    return CourseDraft.model_validate(verifier.get("draft"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lernsequenz")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("praesentation")
    build.add_argument("--course", required=True)
    build.add_argument("--chapter")
    load = sub.add_parser("laden")
    load.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    store = SequenceStore(Path(os.environ["LEARNING_ARTIFACT_ROOT"]))
    if args.command == "praesentation":
        sequence = build_presentation(published_draft(args.course), course_id=args.course, chapter_id=args.chapter)
    else:
        sequence = LernsequenzV1.model_validate_json(args.path.read_text(encoding="utf-8"))
    store.save(sequence)
    print(f"{sequence.sequence_id}: {len(sequence.sections)} Abschnitte, {len(sequence.checkpoints)} Haltepunkte")
    print(_link(sequence.sequence_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
