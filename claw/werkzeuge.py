"""Course tools of the Learning claw (German tool names, German-keyed results).

Every tool returns ``{"ok": True, "daten": {...}}`` or ``{"ok": False,
"fehler": "<German reason>"}`` and never raises. ``daten`` keys:

- ``kurs_erzeugen`` / ``entwurf_ablehnen``: ``job_id``, ``zustand``, ``revision``
- ``kurs_status``: ``job_id``, ``kurs_id``, ``zustand``, ``revision``, ``versuch``
- ``material_importieren``: ``source_id``, ``revision``, ``verarbeitung``
- ``material_status``: ``source_id``, ``kurs_id``, ``revision``, ``verarbeitung``
- ``kurs_lesen``: ``kurs_id``, ``revision``, ``kurs``
- practice and canvas tools: ``zustand`` (the MCP answer's ``state``), ``ergebnis``
  (its ``result`` object, e.g. ``session``, ``hint``, ``tutor``, ``progress``,
  ``canvas``, ``review``) and ``beleg`` (its ``evidence`` or ``None``); an answer
  without ``result`` is reported as unavailable, never passed through
- ``veroeffentlichung_anzeigen``: ``job_id``, ``titel``, ``kapitel``, ``lektionen``,
  ``unbelegte_aussagen``, ``revision``, ``kurs_revision``

``kurs_erzeugen`` and ``sitzung_starten`` reuse their idempotency key for the same
arguments for 600 s, then mint a new one; the other write tools key deterministically.

Author tools send role ``learning.author``; ``entwurf_ablehnen`` sends
``learning.reviewer``.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID, uuid4

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


class Idempotenzfenster:
    """Windowed idempotency keys for "start something new" tools.

    The same arguments reuse one key for ``seconds`` after they were first seen (a
    retry replays instead of starting twice); afterwards a fresh key is minted, so a
    deliberate second identical session or job is possible. In-process only.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, seconds: float = 600) -> None:
        self._clock = clock
        self._seconds = seconds
        self._seen: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def key(self, tool: str, args: dict[str, Any]) -> str:
        digest = idempotency_key(tool, args)
        with self._lock:
            now = self._clock()
            self._seen = {d: v for d, v in self._seen.items() if now - v[1] < self._seconds}
            entry = self._seen.get(digest)
            if entry is None:
                entry = ("claw-" + secrets.token_hex(20), now)
                self._seen[digest] = entry
            return entry[0]


_fenster = Idempotenzfenster()

_MESSAGE_LIMIT = 300
_REASONS = {
    "source_not_ready": "Quelle ist noch nicht verarbeitet",
    "revision_conflict": "Der Stand hat sich geaendert, bitte neu laden",
    "session_revision_conflict": "Der Stand hat sich geaendert, bitte neu laden",
    "canvas_revision_conflict": "Der Stand hat sich geaendert, bitte neu laden",
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
        # One client per call, closed afterwards: the sidecar runs for days.
        with _client() as client:
            return {"ok": True, "daten": action(client)}
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
    """Import material into an existing course. Give exactly one of `pfad` (a path relative to
    the Learning import folder, e.g. "kurs/skript.md") or `url` (a public https address).
    `kurs_revision` is the current revision from `kurs_lesen`; the course revision increases by
    one after the import. Returns `source_id`; then wait with `material_status` until
    `verarbeitung` is "complete".
    """
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
    """Processing state of imported material: `verarbeitung` is "pending", "complete" or
    "failed". Only complete material can be used in `kurs_erzeugen`.
    """
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
    niveau: Literal["beginner", "intermediate", "advanced", "expert"],
    sprache: str,
    lernziele: list[str],
    source_ids: list[str],
    kurs_revision: int,
) -> dict[str, Any]:
    """Start the course factory for an existing course from imported material. `source_ids`
    must be complete (`material_status`); `kurs_revision` is the current revision from
    `kurs_lesen`; `sprache` is a locale such as "de-DE". Generation runs in the background for
    about ten minutes and never publishes. Returns `job_id`; follow it with `kurs_status`.
    """
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
            idempotency_key_value=_fenster.key("kurs_erzeugen", body),
        )
        return _job(data)

    return _run(act)


def kurs_status(job_id: str) -> dict[str, Any]:
    """State of a course factory job. `zustand` runs queued, ingesting, structuring, authoring,
    assessing, verifying, quality_gate, review_ready; it ends in review_ready (ready to show),
    failed, rejected or published. `quality_gate` that does not move on means the quality check
    held the draft; `unbelegte_aussagen` lists claims without a source.
    """
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
    """Read a course: title and its current `revision`. Every write on the course (import,
    factory job, practice session) needs this current revision.
    """
    def act(client: LearningClient) -> dict[str, Any]:
        data = client.get(f"/courses/{_uuid(course_id)}", roles=ROLE_AUTHOR)
        return {"kurs_id": data["course_id"], "revision": data["revision"], "kurs": data["course"]}

    return _run(act)


def entwurf_ablehnen(job_id: str, revision: int, grund: str) -> dict[str, Any]:
    """Reject a review_ready draft with a short reason the person gave. `revision` is the job
    revision from `kurs_status`. Nothing is published.
    """
    body = {"expected_revision": revision, "decision": "reject", "reason": grund}

    def act(client: LearningClient) -> dict[str, Any]:
        job_id_norm = _uuid(job_id)
        data = client.post(
            f"/course-factory/jobs/{job_id_norm}/review",
            body,
            roles=ROLE_REVIEWER,
            idempotency_key_value=idempotency_key(
                "entwurf_ablehnen", {"job_id": job_id_norm, **body}
            ),
        )
        return _job(data)

    return _run(act)


def _anzeigen(client: LearningClient, job_id: str, sitzung: str | None) -> dict[str, Any]:
    job = client.get(f"/course-factory/jobs/{job_id}", roles=ROLE_AUTHOR)
    if job["state"] != _REVIEW_READY:
        raise ValueError("Der Kurs ist noch nicht zur Veroeffentlichung bereit")
    course = client.get(f"/courses/{_uuid(job['course_id'])}", roles=ROLE_AUTHOR)
    revision, kurs_revision = job["revision"], course["revision"]
    if sitzung:
        _freigaben.merken(job_id, revision, kurs_revision, sitzung)
    else:
        logger.warning("Publication display without an MCP session id; it authorizes nothing")
    entwurf = job.get("draft_summary")
    kapitel = _kapitel(entwurf)
    return {
        "job_id": job["job_id"],
        "titel": (entwurf or {}).get("title") or _kurstitel(course),
        # From the verified draft the person approves; absent means unknown, never invented.
        "kapitel": kapitel,
        "lektionen": sum(len(k["lektionen"]) for k in kapitel) if kapitel is not None else None,
        "unbelegte_aussagen": list(job.get("unsupported_claim_ids") or []),
        "revision": revision,
        "kurs_revision": kurs_revision,
    }


def _kurstitel(course: dict[str, Any]) -> str | None:
    # The projection nests the LearnHouse course: {"course": {"course": {...}}}.
    kurs = course.get("course")
    if isinstance(kurs, dict) and isinstance(kurs.get("course"), dict):
        kurs = kurs["course"]
    titel = kurs.get("title") if isinstance(kurs, dict) else None
    return titel if isinstance(titel, str) else None


def _kapitel(entwurf: object) -> list[dict[str, Any]] | None:
    if not isinstance(entwurf, dict) or not isinstance(entwurf.get("chapters"), list):
        return None
    return [
        {
            "titel": kapitel["title"],
            "lektionen": list(kapitel["lessons"]),
            "aufgaben": kapitel["activity_count"],
        }
        for kapitel in entwurf["chapters"]
    ]


def veroeffentlichung_anzeigen(job_id: str, *, mcp_sitzung: str | None = None) -> dict[str, Any]:
    """Return the summary to show the person: title, claims, ``revision``, ``kurs_revision``.

    Only a ``review_ready`` job can be displayed. The sidecar records the display (job,
    both revisions, MCP session, time); there is no token. ``mcp_sitzung`` is filled by
    the server from the MCP request, never by the model.
    """
    try:
        job_id = _uuid(job_id)
    except ValueError as error:
        return {"ok": False, "fehler": f"Ungueltige Eingabe: {error}"[:_MESSAGE_LIMIT]}
    return _run(lambda client: _anzeigen(client, job_id, mcp_sitzung))


_STAND_GEAENDERT = "Der Stand hat sich geaendert, bitte erneut anzeigen"


def veroeffentlichen(
    job_id: str, revision: int, kurs_revision: int, *, mcp_sitzung: str | None = None
) -> dict[str, Any]:
    """Publish a displayed course. Only after the person's yes in a LATER message.

    ``revision`` and ``kurs_revision`` are the two values from the summary of
    ``veroeffentlichung_anzeigen``. Accepted only if that display came from an earlier,
    different MCP session (a person turn in between), less than 10 minutes ago, and job and
    course are still at those revisions. The display is used up (one-time), also when the
    publish call then fails; a retry needs a fresh display.
    """
    if not mcp_sitzung:
        return {"ok": False, "fehler": "Keine MCP-Sitzungskennung - Veroeffentlichung abgelehnt"}
    try:
        job_id_norm = _uuid(job_id)
    except ValueError as error:
        return {"ok": False, "fehler": f"Ungueltige Eingabe: {error}"[:_MESSAGE_LIMIT]}

    def act(client: LearningClient) -> dict[str, Any]:
        try:
            anzeige = _freigaben.einloesen(job_id_norm, revision, kurs_revision, mcp_sitzung)
        except FreigabeAbgelehnt as error:
            raise LearningRejected("freigabe_abgelehnt", error.grund, 0) from error
        job = client.get(f"/course-factory/jobs/{job_id_norm}", roles=ROLE_AUTHOR)
        if job["state"] != _REVIEW_READY:
            raise LearningRejected(
                "freigabe_abgelehnt", "Der Kurs ist nicht zur Veroeffentlichung bereit", 0
            )
        if job["revision"] != revision:
            raise LearningRejected("freigabe_abgelehnt", _STAND_GEAENDERT, 0)
        course = client.get(f"/courses/{_uuid(job['course_id'])}", roles=ROLE_AUTHOR)
        if course["revision"] != kurs_revision:
            raise LearningRejected("freigabe_abgelehnt", _STAND_GEAENDERT, 0)
        ref = anzeige.approval_ref
        body = {"expected_revision": revision, "confirmed": True, "approval_ref": ref}
        data = client.post(
            f"/course-factory/jobs/{job_id_norm}/publish",
            body,
            roles=ROLE_PUBLISHER,
            idempotency_key_value=idempotency_key(
                "veroeffentlichen", {"job_id": job_id_norm, "approval_ref": ref}
            ),
        )
        return _job(data)

    return _run(act)


# --- Practice and canvas tools (Learning MCP) -------------------------------
#
# There is deliberately no tool that saves or submits a canvas: the learner
# does that in the canvas UI, and a submission needs their own confirmation.

_ACTOR = {"actor_id": "learning-claw", "actor_type": "local_user"}
_DEFAULT_BLUEPRINT = {"max_attempts": 3}


# Calls that run a model through OpenFang (grading, tutor, canvas hints) wait
# longer than reads: wrapper 540 s < gateway 600 s < evaluation client 630 s <
# this < the CLI's MCP tool timeout (900 s).
MODEL_TIMEOUT_SECONDS = 660.0


def _mcp(
    event_type: str,
    tool: str,
    *,
    model: bool = False,
    write: bool = False,
    windowed: bool = False,
    course_id: str | None = None,
    session_id: str | None = None,
    expected_revision: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def act(client: LearningClient) -> dict[str, Any]:
        event: dict[str, Any] = {
            "event_type": event_type,
            "invocation_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "actor": dict(_ACTOR),
            "payload": payload or {},
        }
        if course_id is not None:
            event["course_id"] = _uuid(course_id)
        if session_id is not None:
            event["session_id"] = _uuid(session_id)
        if expected_revision is not None:
            event["expected_revision"] = expected_revision
        if write:
            keyed = {k: v for k, v in event.items() if k not in ("invocation_id", "correlation_id")}
            event["idempotency_key"] = (
                _fenster.key(tool, keyed) if windowed else idempotency_key(tool, keyed)
            )

        answer = client.mcp_call(
            tool, event, timeout=MODEL_TIMEOUT_SECONDS if model else None
        )
        result = answer.get("result")
        if not isinstance(result, dict):
            raise LearningUnavailable("Learning lieferte kein Ergebnis")
        return {"zustand": answer.get("state"), "ergebnis": result, "beleg": answer.get("evidence")}

    return _run(act)


def sitzung_starten(
    course_id: str,
    kurs_revision: int,
    modus: Literal["training", "exam"],
    blueprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a practice session for a published course. `modus` is "training" (feedback and
    hints; `blueprint` may set {"max_attempts": 1-500}) or "exam" (no hints; `blueprint` must
    be {"item_count": 1-200}, optionally "item_types"). `kurs_revision` is the current revision
    from `kurs_lesen`. Returns the session with its `session_id` and `revision`.
    """
    return _mcp(
        "learning.session.start",
        "learning_session_start",
        write=True,
        windowed=True,
        course_id=course_id,
        payload={
            "course_revision": kurs_revision,
            "mode": modus,
            "blueprint": dict(blueprint) if blueprint is not None else dict(_DEFAULT_BLUEPRINT),
        },
    )


def naechste_aufgabe(session_id: str) -> dict[str, Any]:
    """Next task in a practice session, chosen by the adaptive engine. Show the task to the
    person; never answer it for them.
    """
    return _mcp("learning.task.next", "learning_task_next", session_id=session_id)


def antwort_geben(session_id: str, revision: int, antwort: dict[str, Any]) -> dict[str, Any]:
    """Submit the person's own answer to the current task. `antwort` is an object such as
    {"text": "..."} with exactly what the person wrote; `revision` is the session revision from
    the last result. Never invent or improve the answer.
    """
    return _mcp(
        "learning.task.answer",
        "learning_task_answer",
        model=True,
        write=True,
        session_id=session_id,
        expected_revision=revision,
        payload={"answer": antwort},
    )


def hinweis(session_id: str, task_id: str) -> dict[str, Any]:
    """A hint for the current task (`task_id` from `naechste_aufgabe`). Only in training mode.
    """
    return _mcp(
        "learning.hint.request",
        "learning_hint_request",
        session_id=session_id,
        payload={"task_id": task_id},
    )


def tutor_fragen(course_id: str, session_id: str, frage: str) -> dict[str, Any]:
    """Ask the course tutor a question of the person. It answers only from the course sources
    and never reveals an expected answer.
    """
    return _mcp(
        "learning.tutor.ask",
        "learning_tutor_ask",
        model=True,
        course_id=course_id,
        session_id=session_id,
        payload={"question": frage},
    )


def fortschritt(session_id: str) -> dict[str, Any]:
    """Learning progress of a practice session (mastery per concept).
    """
    return _mcp("learning.progress.show", "learning_progress_show", session_id=session_id)


def zeichenaufgabe_oeffnen(session_id: str, activity_id: str, session_ref: str) -> dict[str, Any]:
    """Open the drawing canvas (PenEcho) for a canvas task of the session. `activity_id` comes
    from the task; `session_ref` from the session. The person draws; the agent never draws or
    submits.
    """
    return _mcp(
        "learning.canvas.open",
        "learning_canvas_open",
        session_id=session_id,
        payload={"activity_id": activity_id, "session_ref": session_ref},
    )


def zeichenhinweis(
    session_id: str,
    activity_id: str,
    canvas_revision: int,
    session_ref: str,
    objekt_ids: list[str],
) -> dict[str, Any]:
    """A hint about the person's drawing. `canvas_revision` is the current canvas revision;
    `objekt_ids` are the drawing objects the hint is about.
    """
    return _mcp(
        "learning.canvas.hint",
        "learning_canvas_hint",
        model=True,
        session_id=session_id,
        expected_revision=canvas_revision,
        payload={
            "activity_id": activity_id,
            "session_ref": session_ref,
            "selection": {"object_ids": list(objekt_ids)},
        },
    )


def bewertung_zeigen(evaluation_id: str, session_id: str, session_ref: str) -> dict[str, Any]:
    """Show the evaluation of a submitted drawing (`evaluation_id` from the task result).
    """
    return _mcp(
        "learning.canvas.review",
        "learning_canvas_review",
        write=True,
        session_id=session_id,
        payload={"evaluation_id": evaluation_id, "session_ref": session_ref},
    )
