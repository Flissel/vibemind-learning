"""Werkstatt HTTP API: courses, tasks and numbered versions (Fassungen).

Every route needs the author role. Writes also need `X-Werkstatt-Author`, the name that ends
up as `author_ref` on a version. No route waits for a model: `grobentwurf` and `ausformulieren`
only create a job that the Werkstatt worker picks up; the caller polls `/auftraege/{id}`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from spaces.learning.deployment.vibemind_auth import (
    ROLE_AUTHOR,
    ApiError,
    VibeMindRequestContext,
    vibemind_context,
)
from spaces.learning.services.course_factory.application import (
    SourceNotReady,
    current_provenance,
)
from spaces.learning.services.course_factory.artifact_store import CourseFactoryArtifactStore
from spaces.learning.services.course_factory.catalog import SourceCatalog, SourceCatalogLoader
from spaces.learning.services.course_factory.repository import SourceProvenance
from spaces.learning.services.werkstatt.inhalt import fassungs_art, inhalt_lesen
from spaces.learning.services.werkstatt.models import WerkstattAufgabe
from spaces.learning.services.werkstatt.pruefung import aufgabe_pruefen, quellen_aufloesen
from spaces.learning.services.werkstatt.store import WerkstattKonflikt, WerkstattStore
from spaces.learning.services.werkstatt.uebernahme import (
    EntwurfNichtLesbar,
    NichtVeroeffentlicht,
    SchonUebernommen,
    kurs_uebernehmen,
)
from spaces.learning.services.werkstatt.vorschau import vorschau as vorschau_bauen

router = APIRouter(prefix="/api/v1/werkstatt")
Context = Annotated[VibeMindRequestContext, Depends(vibemind_context)]

AUTHOR_HEADER = "X-Werkstatt-Author"
_AUTHOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UNPROCESSABLE = 422  # the Starlette constant was renamed between releases

Kind = Literal["quiz", "open", "case", "cloze", "ordering", "matching"]


@dataclass(frozen=True)
class _Runtime:
    store: WerkstattStore
    session_factory: sessionmaker[Session]


@lru_cache(maxsize=1)
def _runtime() -> _Runtime:
    from spaces.learning.services.db.session import build_session_factory, create_learning_engine

    url = os.environ.get("LEARNING_DATABASE_URL", "").strip()
    if not url:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable",
            "Learning database is not configured",
        )
    factory = build_session_factory(create_learning_engine(url))
    return _Runtime(WerkstattStore(factory), factory)


def _artifact_store(factory: sessionmaker[Session]) -> CourseFactoryArtifactStore:
    root = os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip()
    if not root:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable",
            "Learning artifact root is not configured",
        )
    return CourseFactoryArtifactStore(factory, artifact_root=Path(root))


def _autor(
    author: Annotated[str, Header(alias=AUTHOR_HEADER, max_length=256)] = "",
) -> str:
    if not _AUTHOR.match(author):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "autor_fehlt", f"{AUTHOR_HEADER} is required")
    return author


Autor = Annotated[str, Depends(_autor)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KursAnlegen(_Body):
    course_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    audience: str = Field(min_length=1, max_length=500)
    outcomes: list[str] = Field(min_length=1, max_length=50)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    default_kind: Kind = "quiz"
    hinweise: list[str] = Field(default_factory=list, max_length=50)


class KursAendern(_Body):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    default_kind: Kind | None = None
    reihenfolge: list[str] | None = Field(default=None, max_length=2_000)


class AufgabeAnlegen(_Body):
    chapter_title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=2_000)
    position: int | None = Field(default=None, ge=0)


class Ausformulieren(_Body):
    based_on: int | None = Field(ge=1)
    wunsch: str | None = Field(default=None, max_length=2_000)
    kind: Kind | None = None


class AufgabeAendern(_Body):
    based_on: int | None = Field(ge=1)
    felder: dict[str, Any]
    reason: str = Field(min_length=1, max_length=2_000)


class Zurueckholen(_Body):
    number: int = Field(ge=1)


def _fehler(status_code: int, code: str, message: str) -> ApiError:
    return ApiError(status_code, code, message)


def _konflikt(context: VibeMindRequestContext, neueste: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        {
            "code": "konflikt",
            "message": "Die Aufgabe wurde inzwischen geaendert",
            "retryable": False,
            "correlation_id": str(context.correlation_id),
            "neueste": neueste,
        },
        status_code=status.HTTP_409_CONFLICT,
    )


def _aufgabe(task_id: str, *, schreibend: bool = False) -> WerkstattAufgabe:
    with _runtime().session_factory() as session:
        aufgabe = session.get(WerkstattAufgabe, task_id)
        if aufgabe is None:
            raise _fehler(status.HTTP_404_NOT_FOUND, "aufgabe_unbekannt", "Aufgabe nicht gefunden")
        if schreibend and aufgabe.deleted:
            raise _fehler(status.HTTP_409_CONFLICT, "aufgabe_geloescht", "Die Aufgabe ist geloescht")
        session.expunge(aufgabe)
        return aufgabe


def _kurs(course_id: str) -> dict[str, Any]:
    store = _runtime().store
    try:
        kurs = store.kurs(course_id)
    except LookupError as error:
        raise _fehler(status.HTTP_404_NOT_FOUND, "kurs_unbekannt", "Kurs nicht gefunden") from error
    for eintrag in kurs["aufgaben"]:
        neueste = store.neueste(eintrag["task_id"])
        eintrag["befunde"] = len(neueste["findings"]) if neueste else 0
    return kurs


def _provenance(kurs: dict[str, Any]) -> tuple[SourceProvenance, ...]:
    return tuple(
        SourceProvenance(source_id=str(p["source_id"]), revision=int(p["revision"]),
                         content_hash=str(p["content_hash"]))
        for p in kurs["source_provenance"]
    )


# courses


@router.post("/kurse", status_code=status.HTTP_202_ACCEPTED)
def kurs_anlegen(body: KursAnlegen, context: Context, autor: Autor) -> dict[str, str]:
    context.require(ROLE_AUTHOR)
    runtime = _runtime()
    try:
        provenance = current_provenance(
            runtime.session_factory, body.course_id, tuple(body.source_ids)
        )
    except SourceNotReady as error:
        raise _fehler(
            status.HTTP_409_CONFLICT, "quellen_nicht_bereit", "Quellen sind nicht bereit"
        ) from error
    try:
        runtime.store.kurs_anlegen(
            body.course_id, title=body.title, audience=body.audience,
            default_kind=body.default_kind, outcomes=body.outcomes,
            source_provenance=[
                {"source_id": p.source_id, "revision": p.revision, "content_hash": p.content_hash}
                for p in provenance
            ],
            owner_ref=autor,
        )
    except ValueError as error:
        raise _fehler(status.HTTP_409_CONFLICT, "kurs_existiert", "Der Kurs existiert schon") from error
    auftrag = runtime.store.auftrag_anlegen(
        body.course_id, kind="grobentwurf", input={"hinweise": list(body.hinweise)}
    )
    return {"auftrag_id": auftrag["id"]}


@router.post("/kurse/{course_id}/uebernehmen", status_code=status.HTTP_201_CREATED)
def kurs_uebernehmen_route(course_id: str, context: Context, autor: Autor) -> dict[str, Any]:
    context.require(ROLE_AUTHOR)
    runtime = _runtime()
    try:
        return kurs_uebernehmen(
            runtime.store, _artifact_store(runtime.session_factory), runtime.session_factory,
            course_id, author_ref=autor,
        )
    except NichtVeroeffentlicht as error:
        raise _fehler(
            status.HTTP_404_NOT_FOUND, "nicht_veroeffentlicht",
            "Der Kurs ist nicht veroeffentlicht",
        ) from error
    except SchonUebernommen as error:
        raise _fehler(
            status.HTTP_409_CONFLICT, "schon_uebernommen", "Der Kurs ist schon uebernommen"
        ) from error
    except EntwurfNichtLesbar as error:
        raise _fehler(
            status.HTTP_502_BAD_GATEWAY, "entwurf_nicht_lesbar",
            "Der gespeicherte Entwurf ist nicht lesbar",
        ) from error


@router.get("/kurse/{course_id}")
def kurs_lesen(course_id: str, context: Context) -> dict[str, Any]:
    context.require(ROLE_AUTHOR)
    return _kurs(course_id)


@router.patch("/kurse/{course_id}")
def kurs_aendern(course_id: str, body: KursAendern, context: Context, autor: Autor) -> dict[str, Any]:
    context.require(ROLE_AUTHOR)
    _kurs(course_id)
    store = _runtime().store
    if body.reihenfolge is not None:
        # First: it can fail, and a refused request must change nothing.
        try:
            store.reihenfolge_setzen(course_id, body.reihenfolge)
        except ValueError as error:
            raise _fehler(
                _UNPROCESSABLE, "reihenfolge_unvollstaendig",
                "Die Reihenfolge muss alle Aufgaben des Kurses genau einmal nennen",
            ) from error
    store.kurs_aendern(course_id, title=body.title, default_kind=body.default_kind)
    return _kurs(course_id)


# tasks


@router.post("/kurse/{course_id}/aufgaben", status_code=status.HTTP_201_CREATED)
def aufgabe_anlegen(
    course_id: str, body: AufgabeAnlegen, context: Context, autor: Autor
) -> dict[str, Any]:
    context.require(ROLE_AUTHOR)
    try:
        return _runtime().store.aufgabe_anlegen(
            course_id, chapter_title=body.chapter_title, summary=body.summary, position=body.position
        )
    except LookupError as error:
        raise _fehler(status.HTTP_404_NOT_FOUND, "kurs_unbekannt", "Kurs nicht gefunden") from error


@router.post("/aufgaben/{task_id}/ausformulieren", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def ausformulieren(
    task_id: str, body: Ausformulieren, context: Context, autor: Autor
) -> dict[str, str] | JSONResponse:
    context.require(ROLE_AUTHOR)
    aufgabe = _aufgabe(task_id, schreibend=True)
    store = _runtime().store
    neueste = store.neueste(task_id)
    if (neueste["number"] if neueste else None) != body.based_on:
        return _konflikt(context, neueste or {})
    auftrag = store.auftrag_anlegen(
        aufgabe.course_id, kind="ausformulieren", task_id=task_id,
        input={"based_on": body.based_on, "wunsch": body.wunsch, "kind": body.kind},
    )
    return {"auftrag_id": auftrag["id"]}


def _katalog(kurs: dict[str, Any]) -> SourceCatalog:
    """The course follows the current source state: re-pin its sources, then load the catalog."""
    runtime = _runtime()
    try:
        provenance = current_provenance(
            runtime.session_factory, kurs["course_id"],
            tuple(p.source_id for p in _provenance(kurs)),
        )
        runtime.store.kurs_quellen_setzen(
            kurs["course_id"],
            [
                {"source_id": p.source_id, "revision": p.revision, "content_hash": p.content_hash}
                for p in provenance
            ],
        )
        return SourceCatalogLoader(runtime.session_factory).load(
            course_id=kurs["course_id"], provenance=provenance
        )
    except ValueError as error:  # SourceNotReady, no chunks or too large
        raise _fehler(
            status.HTTP_409_CONFLICT, "quellen_nicht_bereit",
            "Die Quellen des Kurses sind derzeit nicht bereit",
        ) from error


@router.patch("/aufgaben/{task_id}", response_model=None)
def aufgabe_aendern(
    task_id: str, body: AufgabeAendern, context: Context, autor: Autor
) -> dict[str, Any] | JSONResponse:
    context.require(ROLE_AUTHOR)
    aufgabe = _aufgabe(task_id, schreibend=True)
    store = _runtime().store
    neueste = store.neueste(task_id)
    if (neueste["number"] if neueste else None) != body.based_on:
        return _konflikt(context, neueste or {})
    alt = neueste["content"] if neueste else None
    try:
        inhalt = inhalt_lesen({**(alt or {}), **body.felder})
    except ValidationError as error:
        raise _fehler(_UNPROCESSABLE, "inhalt_ungueltig", "Der Inhalt der Aufgabe ist ungueltig") from error
    kurs = store.kurs(aufgabe.course_id)
    katalog = _katalog(kurs)
    # Raw S1 keys must not count as cited: resolve them against the catalog first.
    inhalt, _ = quellen_aufloesen(inhalt, katalog.model_refs, course_id=aufgabe.course_id)
    neu = inhalt.model_dump(mode="json")
    try:
        return store.fassung_anlegen(
            task_id, content=neu, based_on=body.based_on, author_ref=autor, reason=body.reason,
            art=fassungs_art(alt, neu), findings=list(aufgabe_pruefen(inhalt, sources=katalog.snapshots)),
        )
    except WerkstattKonflikt as konflikt:
        return _konflikt(context, konflikt.neueste)
    except ValueError as error:  # deleted between the check above and the write
        raise _fehler(status.HTTP_409_CONFLICT, "aufgabe_geloescht", "Die Aufgabe ist geloescht") from error


@router.delete("/aufgaben/{task_id}")
def aufgabe_loeschen(task_id: str, context: Context, autor: Autor) -> dict[str, Any]:
    context.require(ROLE_AUTHOR)
    _aufgabe(task_id)
    _runtime().store.aufgabe_loeschen(task_id, author_ref=autor)
    return {"task_id": task_id, "deleted": True}


@router.get("/aufgaben/{task_id}/fassungen")
def fassungen(task_id: str, context: Context) -> dict[str, Any]:
    context.require(ROLE_AUTHOR)
    _aufgabe(task_id)
    return {"fassungen": _runtime().store.fassungen(task_id)}


@router.get("/aufgaben/{task_id}/vorschau")
def vorschau(task_id: str, context: Context, fassung: int | None = None) -> dict[str, Any]:
    context.require(ROLE_AUTHOR)
    _aufgabe(task_id)
    alle = _runtime().store.fassungen(task_id)
    if fassung is None:
        gewaehlt = alle[-1] if alle else None
    else:
        gewaehlt = next((f for f in alle if f["number"] == fassung), None)
    if gewaehlt is None:
        raise _fehler(status.HTTP_404_NOT_FOUND, "fassung_unbekannt", "Fassung nicht gefunden")
    return {"number": gewaehlt["number"], **vorschau_bauen(gewaehlt["content"])}


@router.post("/aufgaben/{task_id}/zurueckholen", response_model=None)
def zurueckholen(
    task_id: str, body: Zurueckholen, context: Context, autor: Autor
) -> dict[str, Any] | JSONResponse:
    context.require(ROLE_AUTHOR)
    _aufgabe(task_id)
    try:
        return _runtime().store.zurueckholen(task_id, body.number, author_ref=autor)
    except LookupError as error:
        raise _fehler(status.HTTP_404_NOT_FOUND, "fassung_unbekannt", "Fassung nicht gefunden") from error
    except WerkstattKonflikt as konflikt:
        return _konflikt(context, konflikt.neueste)


# jobs


@router.get("/auftraege/{auftrag_id}")
def auftrag_lesen(auftrag_id: str, context: Context) -> dict[str, Any]:
    context.require(ROLE_AUTHOR)
    try:
        auftrag = _runtime().store.auftrag(auftrag_id)
    except LookupError as error:
        raise _fehler(status.HTTP_404_NOT_FOUND, "auftrag_unbekannt", "Auftrag nicht gefunden") from error
    return {
        "id": auftrag["id"], "kind": auftrag["kind"], "state": auftrag["state"],
        "result": auftrag["result"], "error": auftrag["error"],
    }
