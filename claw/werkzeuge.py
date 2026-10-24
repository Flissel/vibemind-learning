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
from .zeichnung import Zeichnung, ZeichnungStore
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
_zeichnungen = ZeichnungStore()


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
    "no_open_task": "Gerade ist keine Aufgabe offen - erst naechste_aufgabe oder eine neue Sitzung",
    "course_published": "Der Kurs ist schon veroeffentlicht und unveraenderlich - fuer neue Inhalte einen neuen Kurs anlegen",
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
    held the draft: `pruefung_haelt_an` names the reasons (e.g. unsupported_claim,
    claim_missing_citation), `unbelegte_aussagen` lists claims without a source. A held draft
    is not published; start a new job instead.
    """
    def act(client: LearningClient) -> dict[str, Any]:
        data = client.get(f"/course-factory/jobs/{_uuid(job_id)}", roles=ROLE_AUTHOR)
        out = {
            "job_id": data["job_id"],
            "kurs_id": data["course_id"],
            "zustand": data["state"],
            "revision": data["revision"],
            "versuch": data["attempt_number"],
            "unbelegte_aussagen": list(data.get("unsupported_claim_ids") or []),
        }
        if data.get("quality_issue_codes") is not None:
            out["pruefung_haelt_an"] = list(data["quality_issue_codes"])
        if data.get("ai_review_issues"):
            out["ki_pruefung"] = list(data["ai_review_issues"])
        if data.get("ai_review_notes"):
            # Remarks of an AI review that let the draft through; they do not hold it.
            out["ki_hinweise"] = list(data["ai_review_notes"])
        return out

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


def _anzeigen(client: LearningClient, job_id: str) -> dict[str, Any]:
    job = client.get(f"/course-factory/jobs/{job_id}", roles=ROLE_AUTHOR)
    if job["state"] != _REVIEW_READY:
        raise ValueError("Der Kurs ist noch nicht zur Veroeffentlichung bereit")
    course = client.get(f"/courses/{_uuid(job['course_id'])}", roles=ROLE_AUTHOR)
    revision, kurs_revision = job["revision"], course["revision"]
    entwurf = job.get("draft_summary")
    kapitel = _kapitel(entwurf)
    inhalt = {
        "job_id": job["job_id"],
        "titel": (entwurf or {}).get("title") or _kurstitel(course),
        # From the verified draft the person approves; absent means unknown, never invented.
        "kapitel": kapitel,
        "lektionen": sum(len(k["lektionen"]) for k in kapitel) if kapitel is not None else None,
        "unbelegte_aussagen": list(job.get("unsupported_claim_ids") or []),
        "ki_hinweise": list(job.get("ai_review_notes") or []),
        "revision": revision,
        "kurs_revision": kurs_revision,
    }
    anzeige = _freigaben.merken(job_id, revision, kurs_revision, inhalt)
    # The secret goes only to the browser on this machine, never into the model's output:
    # whoever knows it can publish, and the model or its container could send HTTP.
    geoeffnet = _browser_oeffnen(freigabe_link(anzeige.geheimnis))
    return {
        **inhalt,
        "freigabe": (
            "Freigabe-Seite im Browser dieses Rechners geoeffnet"
            if geoeffnet
            else "Freigabe-Seite konnte nicht geoeffnet werden - bitte erneut anzeigen"
        ),
    }


def _browser_oeffnen(url: str) -> bool:
    import webbrowser

    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        logger.warning("Approval page could not be opened in a browser")
        return False


def freigabe_link(geheimnis: str) -> str:
    """Link to the local approval page; only a person in a browser on this machine uses it."""
    host = os.environ.get("LEARNING_CLAW_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("LEARNING_CLAW_MCP_PORT", "8139").strip() or "8139"
    return f"http://{host}:{port}/freigabe/{geheimnis}"


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


def veroeffentlichung_anzeigen(job_id: str) -> dict[str, Any]:
    """Show what would be published: title, chapters with lessons, unsupported claims. Only a
    review_ready job can be shown. It also opens the approval page in the browser on this
    machine (`freigabe` says whether that worked); the person publishes there with a
    button, valid once for 10 minutes. There is no tool that publishes; never claim a
    course is published before `kurs_status` says so.
    """
    try:
        job_id = _uuid(job_id)
    except ValueError as error:
        return {"ok": False, "fehler": f"Ungueltige Eingabe: {error}"[:_MESSAGE_LIMIT]}
    return _run(lambda client: _anzeigen(client, job_id))


_STAND_GEAENDERT = "Der Stand hat sich geaendert, bitte erneut anzeigen"


def freigeben(geheimnis: str) -> dict[str, Any]:
    """Publish after the person pressed the button on the approval page (not a model tool).

    The approval is used up even if publishing then fails; job and course must still be
    at the displayed revisions.
    """

    def act(client: LearningClient) -> dict[str, Any]:
        try:
            anzeige = _freigaben.einloesen(geheimnis)
        except FreigabeAbgelehnt as error:
            raise LearningRejected("freigabe_abgelehnt", error.grund, 0) from error
        job = client.get(f"/course-factory/jobs/{anzeige.job_id}", roles=ROLE_AUTHOR)
        if job["state"] != _REVIEW_READY:
            raise LearningRejected(
                "freigabe_abgelehnt", "Der Kurs ist nicht zur Veroeffentlichung bereit", 0
            )
        if job["revision"] != anzeige.revision:
            raise LearningRejected("freigabe_abgelehnt", _STAND_GEAENDERT, 0)
        course = client.get(f"/courses/{_uuid(job['course_id'])}", roles=ROLE_AUTHOR)
        if course["revision"] != anzeige.kurs_revision:
            raise LearningRejected("freigabe_abgelehnt", _STAND_GEAENDERT, 0)
        ref = anzeige.approval_ref
        body = {"expected_revision": anzeige.revision, "confirmed": True, "approval_ref": ref}
        data = client.post(
            f"/course-factory/jobs/{anzeige.job_id}/publish",
            body,
            roles=ROLE_PUBLISHER,
            idempotency_key_value=idempotency_key(
                "freigeben", {"job_id": anzeige.job_id, "approval_ref": ref}
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
    confirmation: dict[str, Any] | None = None,
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
        if confirmation is not None:
            event["confirmation"] = dict(confirmation)
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
    hints; `blueprint` may set {"max_attempts": 1-500} and "item_types", e.g. ["penecho_canvas"]
    for drawing tasks only) or "exam" (no hints; `blueprint` must
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
    """Submit the person's own answer to the current task. `antwort` is {"text": "..."} for
    open tasks, {"choice_id": "<id>"} for single choice and {"choice_ids": ["<id>", ...]} for
    multiple choice (ids from the task's options), with exactly what the person chose or wrote; `revision` is the session revision from
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


def _canvas_ref(session_id: str) -> str:
    ref = _zeichnungen.session_ref(_uuid(session_id))
    if ref is None:
        raise ValueError("Zuerst die Zeichenaufgabe oeffnen")
    return ref


def zeichen_link(geheimnis: str) -> str:
    """Link to the local drawing page; only a person in a browser on this machine uses it."""
    host = os.environ.get("LEARNING_CLAW_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("LEARNING_CLAW_MCP_PORT", "8139").strip() or "8139"
    return f"http://{host}:{port}/zeichnen/{geheimnis}"


def zeichenaufgabe_oeffnen(session_id: str, activity_id: str) -> dict[str, Any]:
    """Open the drawing canvas for the session's current canvas task (`activity_id` is the
    task id). It opens a drawing page in the browser on this machine (`zeichenflaeche` says
    whether that worked); the person draws there and submits with a button. The agent never
    draws or submits; follow the result with `naechste_aufgabe` or `fortschritt`.
    """
    try:
        session_id, activity_id = _uuid(session_id), _uuid(activity_id)
    except ValueError as error:
        return {"ok": False, "fehler": f"Ungueltige Eingabe: {error}"[:_MESSAGE_LIMIT]}
    session_ref = f"z-{secrets.token_hex(12)}"
    geoeffnet = _mcp(
        "learning.canvas.open",
        "learning_canvas_open",
        session_id=session_id,
        payload={"activity_id": activity_id, "session_ref": session_ref},
    )
    if not geoeffnet.get("ok"):
        return geoeffnet
    aufgabe = ""
    stand = fortschritt(session_id)
    if stand.get("ok"):
        task = (stand["daten"].get("ergebnis", {}).get("session") or {}).get("task") or {}
        aufgabe = str(task.get("prompt") or "")
    geheimnis = _zeichnungen.merken(
        Zeichnung(session_id=session_id, activity_id=activity_id, session_ref=session_ref,
                  aufgabe=aufgabe)
    )
    offen = _browser_oeffnen(zeichen_link(geheimnis))
    return {
        "ok": True,
        "daten": {
            "aufgabe": aufgabe,
            "zeichenflaeche": "Zeichenflaeche im Browser dieses Rechners geoeffnet"
            if offen
            else "Zeichenflaeche konnte nicht geoeffnet werden",
        },
    }


def zeichenhinweis(
    session_id: str,
    activity_id: str,
    canvas_revision: int,
    objekt_ids: list[str],
) -> dict[str, Any]:
    """A hint about the person's drawing. `canvas_revision` is the current canvas revision;
    `objekt_ids` are the drawing objects (strokes) the hint is about.
    """
    try:
        session_ref = _canvas_ref(session_id)
    except ValueError as error:
        return {"ok": False, "fehler": str(error)}
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


def bewertung_zeigen(evaluation_id: str, session_id: str) -> dict[str, Any]:
    """Show the evaluation of a submitted drawing (`evaluation_id` from the result)."""
    try:
        session_ref = _canvas_ref(session_id)
    except ValueError as error:
        return {"ok": False, "fehler": str(error)}
    return _mcp(
        "learning.canvas.review",
        "learning_canvas_review",
        write=True,
        session_id=session_id,
        payload={"evaluation_id": evaluation_id, "session_ref": session_ref},
    )
