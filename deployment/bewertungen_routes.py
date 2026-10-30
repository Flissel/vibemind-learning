"""Course ratings: learners rate a course with a smiley and up to three keywords.

Learner routes are proxied by LearnHouse (service key plus actor header). Owner routes use the
VibeMind service key. No reply to the owner ever carries an actor id.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from fastapi import Request
from fastapi.responses import JSONResponse

from spaces.learning.deployment.lernende_auth import lernende_person
from spaces.learning.deployment.vibemind_auth import ApiError, VibeMindRequestContext, vibemind_context
from spaces.learning.services.bewertungen.store import BewertungFehler, BewertungStore

router = APIRouter()
Context = Annotated[VibeMindRequestContext, Depends(vibemind_context)]

_MELDUNGEN = {
    "smiley_unbekannt": "Bitte wählen Sie eines der drei Gesichter.",
    "zu_viele_stichworte": "Bitte nennen Sie höchstens drei Stichworte.",
    "stichwort_zu_lang": "Ein Stichwort darf höchstens 40 Zeichen lang sein.",
    "course_id_ungueltig": "Der Kurs konnte nicht zugeordnet werden.",
}


@lru_cache(maxsize=1)
def _store() -> BewertungStore:
    from spaces.learning.services.db.session import create_learning_engine

    url = os.environ.get("LEARNING_DATABASE_URL", "").strip()
    if not url:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Learning database is not configured")
    return BewertungStore(create_learning_engine(url))


def _kurs(course_id: str) -> str:
    try:
        return str(UUID(str(course_id).removeprefix("course_")))
    except ValueError as error:
        raise _fehler("course_id_ungueltig") from error


def _fehler(code: str) -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, code, _MELDUNGEN.get(code, code))


@router.post("/api/v1/kursbewertung")
def abgeben(request: Request, payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    actor_id = lernende_person(request)
    kurs = _kurs(payload.get("course_id", ""))
    titel = payload.get("kurs_titel", "")
    smiley = payload.get("smiley", "")
    stichworte = payload.get("stichworte", [])
    if not isinstance(titel, str) or not isinstance(smiley, str) or not isinstance(stichworte, list) \
            or not all(isinstance(wort, str) for wort in stichworte):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "Ungültige Eingabe.")
    try:
        ergebnis = _store().abgeben(kurs, actor_id, titel, smiley, stichworte)
    except BewertungFehler as error:
        raise _fehler(error.code) from error
    return JSONResponse({"gespeichert": True, "neu": ergebnis["neu"]})


@router.get("/api/v1/kursbewertung/{course_id}")
def eigene(request: Request, course_id: str) -> JSONResponse:
    actor_id = lernende_person(request)
    return JSONResponse({"bewertung": _store().eigene(_kurs(course_id), actor_id)})


@router.get("/api/v1/kursbewertungen/neu")
def neue(context: Context) -> JSONResponse:
    return JSONResponse({"bewertungen": _store().neue()})


@router.post("/api/v1/kursbewertungen/gemeldet")
def gemeldet(context: Context, payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    ids = payload.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "ids must be a list of strings")
    return JSONResponse({"markiert": _store().gemeldet_markieren(ids)})


@router.get("/api/v1/kursbewertungen/uebersicht")
def uebersicht(context: Context) -> JSONResponse:
    return JSONResponse({"kurse": _store().uebersicht()})
