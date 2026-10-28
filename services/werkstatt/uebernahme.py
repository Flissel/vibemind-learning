"""Take over a published course as Fassung 1 of every task (one-time, all or nothing)."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from spaces.learning.services.course_factory.artifact_store import CourseFactoryArtifactStore
from spaces.learning.services.course_factory.models import CourseFactoryPublication
from spaces.learning.services.course_factory.schemas import (
    ActivityDraft,
    CourseDraft,
    LessonDraft,
)
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
from spaces.learning.services.werkstatt.store import WerkstattStore

_UEBERNEHMBAR = {"quiz", "open", "case"}


def _lektion(lektionen: list[LessonDraft]) -> dict[str, Any]:
    """One section per chapter. Several lessons are joined: titles with ' / ', each body under
    its own '## title' heading, claims appended, concept ids de-duplicated in order."""
    if len(lektionen) == 1:
        e = lektionen[0]
        title, body = e.title, e.body
    else:
        title = " / ".join(e.title for e in lektionen)[:300]
        body = "\n\n".join(f"## {e.title}\n\n{e.body}" for e in lektionen)
    return {
        "title": title,
        "body": body,
        "claims": [c.model_dump(mode="json") for e in lektionen for c in e.claims],
        "concept_ids": list(dict.fromkeys(i for e in lektionen for i in e.concept_ids)),
    }


def _inhalt(a: ActivityDraft, draft: CourseDraft) -> dict[str, Any]:
    zitate = [c.model_dump(mode="json") for c in draft.citations if c.citation_id in a.citation_ids]
    inhalt: dict[str, Any] = {
        "kind": a.type, "prompt": a.prompt, "concept_ids": list(a.concept_ids),
        "citation_ids": list(a.citation_ids), "zitate": zitate,
    }
    if a.type == "quiz":
        inhalt["options"] = [o.model_dump(mode="json") for o in a.options]
        inhalt["correct_option_ids"] = list(a.correct_option_ids)
    else:
        inhalt["expected_answer"] = a.expected_answer
        inhalt["rubric"] = [r.model_dump(mode="json") for r in a.rubric]
    return inhalt


def kurs_uebernehmen(
    store: WerkstattStore,
    artifact_store: CourseFactoryArtifactStore,
    session_factory: sessionmaker[Session],
    course_id: str,
    *,
    author_ref: str,
) -> dict[str, int]:
    try:
        store.kurs(course_id)
    except LookupError:
        pass
    else:
        raise ValueError("schon_uebernommen")
    with session_factory() as session:
        veroeffentlichung = session.scalars(
            select(CourseFactoryPublication)
            .where(CourseFactoryPublication.course_id == course_id)
            .order_by(CourseFactoryPublication.created_at.desc())
            .limit(1)
        ).first()
        if veroeffentlichung is None:
            raise LookupError("nicht_veroeffentlicht")
        job_id, versuch = veroeffentlichung.job_id, veroeffentlichung.attempt_number
    try:
        ausgabe = artifact_store.read_stage_output(
            job_id, attempt_number=versuch, stage=FactoryState.VERIFYING
        )
        draft = CourseDraft.model_validate(dict(ausgabe["source_verifier"])["draft"])  # type: ignore[call-overload]
    except (LookupError, KeyError, TypeError, ValidationError) as error:
        raise LookupError("nicht_veroeffentlicht") from error

    aktivitaeten = {a.activity_id: a for a in draft.activities}
    lektionen = {e.lesson_id: e for e in draft.lessons}
    aufgaben: list[dict[str, Any]] = []
    uebersprungen = 0
    kapitel = 0
    for ch in draft.chapters:
        erste = True
        angelegt = 0
        for aid in ch.activity_ids:
            a = aktivitaeten.get(aid)
            if a is None or a.type not in _UEBERNEHMBAR:
                uebersprungen += 1
                continue
            inhalt = _inhalt(a, draft)
            if erste:
                gehoerig = [lektionen[i] for i in ch.lesson_ids if i in lektionen]
                if gehoerig:
                    inhalt["lektion"] = _lektion(gehoerig)
            try:
                inhalt = inhalt_lesen(inhalt).model_dump(mode="json")
            except ValidationError:
                uebersprungen += 1  # does not fit the Werkstatt schema; counted, not silent
                continue
            erste = False
            angelegt += 1
            aufgaben.append(
                {"chapter_title": ch.title, "summary": a.prompt[:120], "content": inhalt}
            )
        kapitel += 1 if angelegt else 0

    store.uebernahme_anwenden(
        course_id, title=draft.title, audience="", default_kind="quiz",
        outcomes=list(draft.outcomes),
        source_provenance=[p.model_dump(mode="json") for p in draft.source_provenance],
        owner_ref=author_ref, aufgaben=aufgaben, author_ref=author_ref,
    )
    return {"aufgaben": len(aufgaben), "kapitel": kapitel, "uebersprungen": uebersprungen}
