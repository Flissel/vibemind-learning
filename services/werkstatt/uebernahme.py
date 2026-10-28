"""Take over a published course as Fassung 1 of every task (one-time, all or nothing)."""

from __future__ import annotations

from collections.abc import Callable
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
from spaces.learning.services.course_factory.verification import SourceSnapshot
from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
from spaces.learning.services.werkstatt.pruefung import aufgabe_pruefen
from spaces.learning.services.werkstatt.store import WerkstattStore

_UEBERNEHMBAR = {"quiz", "open", "case"}


class SchonUebernommen(ValueError):
    """The course already exists in the Werkstatt."""


class NichtVeroeffentlicht(LookupError):
    """No publication (or no draft belonging to it) exists for the course."""


class EntwurfNichtLesbar(Exception):
    """The stored draft exists but cannot be read or validated (hash, JSON, path, schema)."""


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


def _inhalt(
    a: ActivityDraft, draft: CourseDraft, lektion: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The task content; `zitate` holds every citation the activity OR the attached lesson's
    claims refer to, so the first edit does not strip the lesson's sources."""
    zitiert = set(a.citation_ids)
    if lektion is not None:
        zitiert |= {i for c in lektion["claims"] for i in c["citation_ids"]}
    zitate = [c.model_dump(mode="json") for c in draft.citations if c.citation_id in zitiert]
    inhalt: dict[str, Any] = {
        "kind": a.type, "prompt": a.prompt, "concept_ids": list(a.concept_ids),
        "citation_ids": list(a.citation_ids), "zitate": zitate,
    }
    if lektion is not None:
        inhalt["lektion"] = lektion
    if a.type == "quiz":
        inhalt["options"] = [o.model_dump(mode="json") for o in a.options]
        inhalt["correct_option_ids"] = list(a.correct_option_ids)
    else:
        inhalt["expected_answer"] = a.expected_answer
        inhalt["rubric"] = [r.model_dump(mode="json") for r in a.rubric]
    return inhalt


def _kurzer_fehler(error: ValidationError) -> str:
    erster = error.errors()[0]
    ort = ".".join(str(t) for t in erster["loc"])
    return f"{ort}: {erster['msg']}"[:200]


def _entwurf_laden(
    artifact_store: CourseFactoryArtifactStore,
    session_factory: sessionmaker[Session],
    course_id: str,
) -> CourseDraft:
    """The verified draft of the newest publication of the course."""
    with session_factory() as session:
        veroeffentlichung = session.scalars(
            select(CourseFactoryPublication)
            .where(CourseFactoryPublication.course_id == course_id)
            .order_by(CourseFactoryPublication.created_at.desc())
            .limit(1)
        ).first()
        if veroeffentlichung is None:
            raise NichtVeroeffentlicht("nicht_veroeffentlicht")
        job_id, versuch = veroeffentlichung.job_id, veroeffentlichung.attempt_number
    try:
        ausgabe = artifact_store.read_stage_output(
            job_id, attempt_number=versuch, stage=FactoryState.VERIFYING
        )
        return CourseDraft.model_validate(dict(ausgabe["source_verifier"])["draft"])  # type: ignore[call-overload]
    except (LookupError, ValueError, OSError, KeyError, TypeError) as error:
        # ValidationError is a ValueError; read/parse/integrity failures all land here.
        raise EntwurfNichtLesbar("entwurf_nicht_lesbar") from error


def kurs_uebernehmen(
    store: WerkstattStore,
    artifact_store: CourseFactoryArtifactStore,
    session_factory: sessionmaker[Session],
    course_id: str,
    *,
    author_ref: str,
    quellen_laden: Callable[[str, tuple[str, ...]], tuple[SourceSnapshot, ...]] | None = None,
) -> dict[str, Any]:
    """`quellen_laden(course_id, source_ids)` returns the current source snapshots for the
    checks of Fassung 1. Without it, or if it raises (stale or missing sources), the takeover
    still succeeds with the checks skipped and `pruefung_uebersprungen: True` in the result."""
    try:
        store.kurs(course_id)
    except LookupError:
        pass
    else:
        raise SchonUebernommen("schon_uebernommen")
    draft = _entwurf_laden(artifact_store, session_factory, course_id)

    snapshots: tuple[SourceSnapshot, ...] | None = None
    if quellen_laden is not None:
        try:
            snapshots = quellen_laden(
                course_id, tuple(p.source_id for p in draft.source_provenance)
            )
        except (ValueError, LookupError, OSError):
            snapshots = None

    aktivitaeten = {a.activity_id: a for a in draft.activities}
    lektionen = {e.lesson_id: e for e in draft.lessons}
    aufgaben: list[dict[str, Any]] = []
    details: list[dict[str, str]] = []
    verloren: list[dict[str, str]] = []
    kapitel = 0
    for ch in draft.chapters:
        erste = True
        angelegt = 0
        gehoerig = [lektionen[i] for i in ch.lesson_ids if i in lektionen]
        for aid in ch.activity_ids:
            a = aktivitaeten.get(aid)
            if a is None:
                details.append({"activity_id": aid, "grund": "aktivitaet_fehlt"})
                continue
            if a.type not in _UEBERNEHMBAR:
                details.append({"activity_id": aid, "grund": "typ_nicht_uebernehmbar"})
                continue
            traegt_lektion = erste and bool(gehoerig)
            inhalt = _inhalt(a, draft, _lektion(gehoerig) if traegt_lektion else None)
            try:
                gelesen = inhalt_lesen(inhalt)
                inhalt = gelesen.model_dump(mode="json")
            except ValidationError as error:
                details.append({
                    "activity_id": aid, "grund": f"inhalt_ungueltig: {_kurzer_fehler(error)}",
                })
                continue
            erste = False
            angelegt += 1
            befunde = list(aufgabe_pruefen(gelesen, sources=snapshots)) if snapshots is not None else []
            aufgaben.append({
                "chapter_title": ch.title, "summary": a.prompt[:120], "content": inhalt,
                "findings": befunde,
                "herkunft_key": aid,
                "lektion_herkunft_key": gehoerig[0].lesson_id if traegt_lektion else None,
            })
        if angelegt:
            kapitel += 1
        elif gehoerig:
            verloren.extend(
                {"lesson_id": e.lesson_id, "grund": "lektion_ohne_aufgabe"} for e in gehoerig
            )

    try:
        store.uebernahme_anwenden(
            course_id, title=draft.title, audience="", default_kind="quiz",
            outcomes=list(draft.outcomes),
            source_provenance=[p.model_dump(mode="json") for p in draft.source_provenance],
            owner_ref=author_ref, aufgaben=aufgaben, author_ref=author_ref,
        )
    except ValueError as error:
        if str(error) == "schon_uebernommen":  # lost a race with a second takeover
            raise SchonUebernommen("schon_uebernommen") from error
        raise
    ergebnis: dict[str, Any] = {
        "aufgaben": len(aufgaben), "kapitel": kapitel, "uebersprungen": len(details),
        "uebersprungen_details": details, "lektionen_verloren": verloren,
    }
    if snapshots is None:
        ergebnis["pruefung_uebersprungen"] = True
    return ergebnis


def herkunft_nachtragen(
    store: WerkstattStore,
    artifact_store: CourseFactoryArtifactStore,
    session_factory: sessionmaker[Session],
    course_id: str,
) -> dict[str, Any]:
    """Fill the origin keys of an already taken-over course. Each task is matched by the exact
    prompt of its Fassung 1 against the activities of the newest published draft; the task that
    carries the chapter lesson (`lektion` in Fassung 1) gets the chapter's first lesson id.
    Only empty columns are filled, so a second run changes nothing."""
    kurs = store.kurs(course_id)  # LookupError("kurs_unbekannt") for an unknown course
    draft = _entwurf_laden(artifact_store, session_factory, course_id)
    aus_entwurf: dict[str, list[Any]] = {}
    for a in draft.activities:
        aus_entwurf.setdefault(a.prompt, []).append(a)
    lektionen = {e.lesson_id for e in draft.lessons}
    kapitel_von: dict[str, Any] = {}
    for ch in draft.chapters:
        for aid in ch.activity_ids:
            kapitel_von.setdefault(aid, ch)
    inhalte: dict[str, dict[str, Any]] = {}
    for aufgabe in kurs["aufgaben"]:
        erste = next((f for f in store.fassungen(aufgabe["task_id"]) if f["number"] == 1), None)
        inhalte[aufgabe["task_id"]] = erste["content"] if erste else {}
    # A prompt shared by several tasks cannot be told apart: none of them is assigned.
    prompts_der_aufgaben = [i.get("prompt") for i in inhalte.values() if isinstance(i.get("prompt"), str)]
    vergeben = {a["herkunft_key"] for a in kurs["aufgaben"] if a["herkunft_key"]}
    zuordnung: dict[str, tuple[str | None, str | None]] = {}
    ohne: list[str] = []
    schon = 0
    for aufgabe in kurs["aufgaben"]:
        if aufgabe["herkunft_key"]:
            schon += 1
            continue
        inhalt = inhalte[aufgabe["task_id"]]
        prompt = inhalt.get("prompt")
        treffer = aus_entwurf.get(prompt, []) if isinstance(prompt, str) else []
        if len(treffer) != 1 or prompts_der_aufgaben.count(prompt) != 1:
            ohne.append(aufgabe["task_id"])
            continue
        aktivitaet = treffer[0]
        if aktivitaet.activity_id in vergeben:
            ohne.append(aufgabe["task_id"])
            continue
        vergeben.add(aktivitaet.activity_id)
        lektion_key: str | None = None
        if inhalt.get("lektion") is not None:
            kap = kapitel_von.get(aktivitaet.activity_id)
            lektion_key = next((i for i in (kap.lesson_ids if kap else []) if i in lektionen), None)
        zuordnung[aufgabe["task_id"]] = (aktivitaet.activity_id, lektion_key)
    geschrieben = store.herkunft_setzen(zuordnung)
    return {"zugeordnet": geschrieben, "schon_zugeordnet": schon, "ohne_zuordnung": ohne}
