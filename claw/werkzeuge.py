"""Course tools of the Learning claw (German tool names, German-keyed results).

Every tool returns ``{"ok": True, "daten": {...}}`` or ``{"ok": False,
"fehler": "<German reason>"}`` and never raises. ``daten`` keys:

- ``kurs_erzeugen`` / ``entwurf_ablehnen``: ``job_id``, ``zustand``, ``revision``
- ``kurs_status``: ``job_id``, ``kurs_id``, ``zustand``, ``revision``, ``versuch``
- ``material_importieren``: ``source_id``, ``revision``, ``verarbeitung``
- ``material_status``: ``source_id``, ``kurs_id``, ``revision``, ``verarbeitung``
- ``kurs_lesen``: ``kurs_id``, ``revision``, ``kurs``

Author tools send role ``learning.author``; ``entwurf_ablehnen`` sends
``learning.reviewer``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx

from .freigabe import FreigabeAbgelehnt, FreigabeStore
from .learning_client import (
    LearningClient,
    LearningRejected,
    LearningUnavailable,
    idempotency_key,
)


logger = logging.getLogger(__name__)

ROLE_AUTHOR = "learning.author"
ROLE_REVIEWER = "learning.reviewer"
ROLE_PUBLISHER = "learning.publisher"
_REVIEW_READY = "review_ready"

# Module-level so tests can swap in a store with a fake clock.
_freigaben = FreigabeStore()

_MESSAGE_LIMIT = 300
_REASONS = {
    "source_not_ready": "Quelle ist noch nicht verarbeitet",
    "revision_conflict": "Der Stand hat sich geaendert, bitte neu laden",
    "not_found": "Nicht gefunden",
    "idempotency_conflict": "Gleiche Anfrage mit anderem Inhalt",
}


def _client() -> LearningClient:
    return LearningClient(
        os.environ.get("LEARNING_CLAW_API_URL", "http://127.0.0.1:8090/api/v1"),
        os.environ.get("LEARNING_CLAW_MCP_URL", "http://127.0.0.1:8091/mcp"),
        os.environ.get("LEARNING_VIBEMIND_SERVICE_KEY", ""),
    )


def _run(action: Callable[[LearningClient], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"ok": True, "daten": action(_client())}
    except LearningRejected as error:
        reason = _REASONS.get(error.code) or (error.message or error.code)[:_MESSAGE_LIMIT]
        logger.info("Learning rejected a claw call: %s", error.code)
        return {"ok": False, "fehler": reason}
    except LearningUnavailable as error:
        return {"ok": False, "fehler": str(error)}
    except httpx.HTTPError:
        return {"ok": False, "fehler": "Learning ist nicht erreichbar"}
    except (ValueError, KeyError) as error:
        return {"ok": False, "fehler": f"Ungueltige Eingabe oder Antwort: {error}"[:_MESSAGE_LIMIT]}


def _uuid(value: str) -> str:
    return str(UUID(str(value)))


def _job(data: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": data["job_id"], "zustand": data["state"], "revision": data["revision"]}


def material_importieren(
    course_id: str,
    titel: str,
    kurs_revision: int,
    pfad: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    if (pfad is None) == (url is None):
        return {"ok": False, "fehler": "Genau eines von pfad oder url angeben"}
    source = (
        {"kind": "local_import", "relative_path": pfad}
        if pfad is not None
        else {"kind": "https", "url": url}
    )
    body = {
        "course_id": course_id,
        "expected_course_revision": kurs_revision,
        "title": titel,
        "source": source,
    }

    def act(client: LearningClient) -> dict[str, Any]:
        data = client.post(
            "/materials/import",
            body,
            roles=ROLE_AUTHOR,
            idempotency_key_value=idempotency_key("material_importieren", body),
        )
        return {
            "source_id": data["source_id"],
            "revision": data["revision"],
            "verarbeitung": data["projection_state"],
        }

    return _run(act)


def material_status(source_id: str) -> dict[str, Any]:
    def act(client: LearningClient) -> dict[str, Any]:
        data = client.get(f"/materials/{_uuid(source_id)}", roles=ROLE_AUTHOR)
        return {
            "source_id": data["source_id"],
            "kurs_id": data["course_id"],
            "revision": data["revision"],
            "verarbeitung": data["projection_state"],
        }

    return _run(act)


def kurs_erzeugen(
    course_id: str,
    titel: str,
    zielgruppe: str,
    niveau: str,
    sprache: str,
    lernziele: list[str],
    source_ids: list[str],
    kurs_revision: int,
) -> dict[str, Any]:
    body = {
        "course_id": course_id,
        "expected_course_revision": kurs_revision,
        "title": titel,
        "audience": zielgruppe,
        "level": niveau,
        "locale": sprache,
        "learning_objectives": list(lernziele),
        "source_ids": list(source_ids),
    }

    def act(client: LearningClient) -> dict[str, Any]:
        data = client.post(
            "/course-factory/jobs",
            body,
            roles=ROLE_AUTHOR,
            idempotency_key_value=idempotency_key("kurs_erzeugen", body),
        )
        return _job(data)

    return _run(act)


def kurs_status(job_id: str) -> dict[str, Any]:
    def act(client: LearningClient) -> dict[str, Any]:
        data = client.get(f"/course-factory/jobs/{_uuid(job_id)}", roles=ROLE_AUTHOR)
        return {
            "job_id": data["job_id"],
            "kurs_id": data["course_id"],
            "zustand": data["state"],
            "revision": data["revision"],
            "versuch": data["attempt_number"],
        }

    return _run(act)


def kurs_lesen(course_id: str) -> dict[str, Any]:
    def act(client: LearningClient) -> dict[str, Any]:
        data = client.get(f"/courses/{_uuid(course_id)}", roles=ROLE_AUTHOR)
        return {"kurs_id": data["course_id"], "revision": data["revision"], "kurs": data["course"]}

    return _run(act)


def entwurf_ablehnen(job_id: str, revision: int, grund: str) -> dict[str, Any]:
    body = {"expected_revision": revision, "decision": "reject", "reason": grund}

    def act(client: LearningClient) -> dict[str, Any]:
        data = client.post(
            f"/course-factory/jobs/{_uuid(job_id)}/review",
            body,
            roles=ROLE_REVIEWER,
            idempotency_key_value=idempotency_key("entwurf_ablehnen", {"job_id": job_id, **body}),
        )
        return _job(data)

    return _run(act)


def _anzeigen(client: LearningClient, job_id: str) -> dict[str, Any]:
    job = client.get(f"/course-factory/jobs/{job_id}", roles=ROLE_AUTHOR)
    if job["state"] != _REVIEW_READY:
        raise ValueError("Der Kurs ist noch nicht zur Veroeffentlichung bereit")
    kurs = client.get(f"/courses/{_uuid(job['course_id'])}", roles=ROLE_AUTHOR)["course"]
    titel = kurs.get("title") if isinstance(kurs, dict) else None
    return {
        "job_id": job["job_id"],
        "titel": titel,
        # The course projection does not carry these; never invented.
        "kapitel": None,
        "lektionen": None,
        "unbelegte_aussagen": list(job.get("unsupported_claim_ids") or []),
        "revision": job["revision"],
        "token": _freigaben.ausstellen(job_id, job["revision"]),
    }


def veroeffentlichung_anzeigen(job_id: str) -> dict[str, Any]:
    """Return the summary to show the person plus a one-time publication token.

    Only a ``review_ready`` job can be displayed. The token is valid 10 minutes
    and bound to (job, revision).
    """
    try:
        job_id = _uuid(job_id)
    except ValueError as error:
        return {"ok": False, "fehler": f"Ungueltige Eingabe: {error}"[:_MESSAGE_LIMIT]}
    return _run(lambda client: _anzeigen(client, job_id))


def veroeffentlichen(job_id: str, token: str) -> dict[str, Any]:
    """Publish a displayed course. Call only after the person said yes post-display.

    The token is consumed (one-time use) before the publish call, so a failed or
    unavailable publish needs a fresh display; a retry with the same token is
    refused. The idempotency key derives from (job_id, token).
    """

    def act(client: LearningClient) -> dict[str, Any]:
        job_id_norm = _uuid(job_id)
        job = client.get(f"/course-factory/jobs/{job_id_norm}", roles=ROLE_AUTHOR)
        try:
            _freigaben.einloesen(job_id_norm, job["revision"], token)
        except FreigabeAbgelehnt as error:
            raise LearningRejected("freigabe_abgelehnt", error.grund, 0) from error
        if job["state"] != _REVIEW_READY:
            raise ValueError("Der Kurs ist nicht zur Veroeffentlichung bereit")
        body = {"expected_revision": job["revision"], "confirmed": True, "approval_ref": token}
        data = client.post(
            f"/course-factory/jobs/{job_id_norm}/publish",
            body,
            roles=ROLE_PUBLISHER,
            idempotency_key_value=idempotency_key(
                "veroeffentlichen", {"job_id": job_id_norm, "token": token}
            ),
        )
        return _job(data)

    return _run(act)
