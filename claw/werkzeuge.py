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
- ``werkstatt_freigabe_anzeigen``: ``course_id``, ``titel``, ``nummer``, ``vergleich``,
  ``befunde``, ``blockiert``, ``nichts_zu_veroeffentlichen``, ``link_geoeffnet``,
  ``handy_link_gesendet``

``kurs_erzeugen`` and ``sitzung_starten`` reuse their idempotency key for the same
arguments for 600 s, then mint a new one; the other write tools key deterministically.

Author tools send role ``learning.author``; ``entwurf_ablehnen`` sends
``learning.reviewer``.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx

from .freigabe import WERKSTATT, FreigabeAbgelehnt, FreigabeStore, handy_basis
from .pruefung_seite import PREFIX as PRUEFUNG_PREFIX
from .pruefung_seite import PruefungStore
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
_pruefungen = PruefungStore()


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
    "konflikt": "Die Aufgabe wurde inzwischen geaendert - die neueste Fassung steht in `neueste`",
    "freigabe_nicht_offen": "Diese Freigabe ist nicht mehr offen - bitte erneut anzeigen",
    "learnhouse_stand_veraltet": "Der Kurs wurde in LearnHouse inzwischen geaendert - bitte erneut anzeigen",
    "nichts_zu_veroeffentlichen": "Seit der letzten Freigabe hat sich nichts geaendert",
    "freigabe_blockiert": "Die Freigabe ist durch Befunde blockiert",
    "freigabe_id_reused": "Diese Freigabe wurde in LearnHouse schon verwendet - bitte erneut anzeigen",
    "kurs_unbekannt": "Kurs nicht gefunden",
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
        out: dict[str, Any] = {"ok": False, "fehler": reason}
        if error.details.get("neueste") is not None:
            out["neueste"] = error.details["neueste"]
        return out
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
    hinweise: list[str] | None = None,
) -> dict[str, Any]:
    """Start the course factory for an existing course from imported material. `source_ids`
    must be complete (`material_status`); `kurs_revision` is the current revision from
    `kurs_lesen`; `sprache` is a locale such as "de-DE". `hinweise` (optional, at most 10 short
    texts) are the person's notes for this run, e.g. why a previous draft was rejected; every
    role of the factory addresses them. Generation runs in the background for about ten minutes
    and never publishes. Returns `job_id`; follow it with `kurs_status`.
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
    if hinweise:
        body["reviewer_notes"] = list(hinweise)

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


# --- Human review of held AI evaluations ------------------------------------

_MAX_PRUEFUNGEN = 20


def quellen_frische() -> dict[str, Any]:
    """Source pages whose visible text changed since a course was built from them (found by
    the nightly check). `geaendert` lists `kurs_id`, `url`, `geaendert_am`, `hinweis`; an
    empty list means every watched source still reads as it did.
    """
    def act(client: LearningClient) -> dict[str, Any]:
        data = client.get("/quellen/frische", roles=ROLE_AUTHOR)
        return {"geaendert": [{
            "kurs_id": item["course_id"], "url": item["url"], "geaendert_am": item["changed_at"],
            "zuletzt_geprueft": item.get("last_checked_at"), "hinweis": item.get("note", ""),
        } for item in data.get("changed", [])]}

    return _run(act)


def pruefungen_anzeigen() -> dict[str, Any]:
    """List AI evaluations held for a person (e.g. the AI was unsure) and open the review
    page in the browser on this machine (`pruefseite` says whether that worked). The person
    confirms the AI score or sets their own points there; there is no tool that decides a
    review, and the learner's mastery only counts after that decision.
    """

    def act(client: LearningClient) -> dict[str, Any]:
        offen = client.get("/reviews", roles=ROLE_REVIEWER)["reviews"][:_MAX_PRUEFUNGEN]
        eintraege = []
        for review in offen:
            detail = client.get(f"/reviews/{_uuid(review['review_id'])}", roles=ROLE_REVIEWER)
            eintraege.append({
                "review_id": detail["review_id"],
                "grund": detail.get("reason"),
                "aufgabe": detail.get("task_prompt"),
                "antwort": detail.get("answer_text"),
                "erwartet": detail.get("expected_answer"),
                "kriterien": detail.get("rubric") or [],
                "ki_punkte": detail.get("ai_score"),
                "ki_sicherheit": detail.get("ai_confidence"),
                "zeichnung_png_base64": detail.get("drawing_png_base64"),
            })
        ueberblick = [
            {k: e[k] for k in ("review_id", "grund", "aufgabe", "ki_punkte", "ki_sicherheit")}
            | {"zeichnung": bool(e["zeichnung_png_base64"])}
            for e in eintraege
        ]
        if not eintraege:
            return {"offen": 0, "pruefungen": [], "pruefseite": "Keine offenen Pruefungen"}
        anzeige = _pruefungen.merken(eintraege)
        # Like the approval page: the secret goes only to the local browser, never to the model.
        geoeffnet = _browser_oeffnen(pruefung_link(anzeige.geheimnis))
        return {
            "offen": len(eintraege),
            "pruefungen": ueberblick,
            "pruefseite": (
                "Pruefseite im Browser dieses Rechners geoeffnet"
                if geoeffnet
                else "Pruefseite konnte nicht geoeffnet werden - bitte erneut anzeigen"
            ),
        }

    return _run(act)


def pruefung_link(geheimnis: str) -> str:
    return freigabe_link(geheimnis).replace("/freigabe/", PRUEFUNG_PREFIX, 1)


def pruefung_entscheiden(
    geheimnis: str, review_id: str, entscheidung: str, punkte: str, notiz: str
) -> dict[str, Any]:
    """Record the person's decision from the review page (not a model tool)."""

    def act(client: LearningClient) -> dict[str, Any]:
        if entscheidung not in {"confirm", "override"}:
            raise ValueError("unbekannte Entscheidung")
        body: dict[str, Any] = {"decision": entscheidung}
        if entscheidung == "override":
            prozent = float(punkte)
            if not 0 <= prozent <= 100:
                raise ValueError("Punkte muessen zwischen 0 und 100 liegen")
            body["score"] = prozent / 100
        if notiz.strip():
            body["note"] = notiz.strip()[:2_000]
        try:
            _pruefungen.einloesen(geheimnis, _uuid(review_id))
        except FreigabeAbgelehnt as error:
            raise LearningRejected("pruefung_abgelehnt", error.grund, 0) from error
        data = client.post(f"/reviews/{_uuid(review_id)}/decision", body, roles=ROLE_REVIEWER)
        return {
            "review_id": data["review_id"],
            "entscheidung": data["decision"],
            "punkte": data["score"],
            "lernstand_angerechnet": bool(data.get("mastery_applied")),
        }

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


# --- Werkstatt: courses and tasks as numbered versions -----------------------
#
# Every write names the claw as author. Nothing here publishes: changes reach learners
# only after the owner's release, which the claw cannot do. Jobs run in the background.

_WERKSTATT = "/werkstatt"
_AUTOR = {"X-Werkstatt-Author": "claw"}


def _seg(value: str) -> str:
    return quote(str(value), safe="")


def _ws_get(path: str) -> dict[str, Any]:
    return _run(lambda client: client.get(_WERKSTATT + path, roles=ROLE_AUTHOR))


def _ws_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _run(
        lambda client: client.post(_WERKSTATT + path, body, roles=ROLE_AUTHOR, extra=_AUTOR)
    )


def _ws_patch(path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _run(
        lambda client: client.patch(_WERKSTATT + path, body, roles=ROLE_AUTHOR, extra=_AUTOR)
    )


def werkstatt_kurs_neu(
    course_id: str,
    titel: str,
    zielgruppe: str,
    lernziele: list[str],
    source_ids: list[str],
    standard_typ: str | None = None,
    hinweise: list[str] | None = None,
    fach: str = "terminal",
) -> dict[str, Any]:
    """Legt einen Werkstatt-Kurs an und startet den Grobentwurf aus den Quellen
    (`standard_typ`: quiz, open, case, cloze, ordering, matching, number oder sketch; `fach`:
    "terminal" ist die Vorgabe, "mathe" fuer Mathe-Kurse). Gibt `auftrag_id`
    zurueck. Auftraege laufen im Hintergrund; mit werkstatt_auftrag nachsehen.
    """
    body: dict[str, Any] = {
        "course_id": course_id, "title": titel, "audience": zielgruppe,
        "outcomes": list(lernziele), "source_ids": list(source_ids),
    }
    if standard_typ is not None:
        body["default_kind"] = standard_typ
    if fach != "terminal":
        body["fach"] = fach
    if hinweise:
        body["hinweise"] = list(hinweise)
    return _ws_post("/kurse", body)


def werkstatt_grobentwurf_neu(course_id: str, hinweise: list[str] | None = None) -> dict[str, Any]:
    """Startet den Grobentwurf eines Werkstatt-Kurses neu, etwa nachdem der erste Auftrag
    fehlgeschlagen ist. Geht nur, solange der Kurs keine Aufgaben hat und kein Grobentwurf
    wartet oder laeuft. Gibt `auftrag_id` zurueck. Auftraege laufen im Hintergrund; mit
    werkstatt_auftrag nachsehen.
    """
    body: dict[str, Any] = {}
    if hinweise:
        body["hinweise"] = list(hinweise)
    return _ws_post(f"/kurse/{_seg(course_id)}/grobentwurf", body)


def werkstatt_kurs_zeigen(course_id: str) -> dict[str, Any]:
    """Zeigt einen Werkstatt-Kurs mit seinen Aufgaben, Reihenfolge und der Zahl offener Befunde."""
    return _ws_get(f"/kurse/{_seg(course_id)}")


def werkstatt_kurs_aendern(
    course_id: str,
    titel: str | None = None,
    standard_typ: str | None = None,
    reihenfolge: list[str] | None = None,
) -> dict[str, Any]:
    """Aendert Titel, Standard-Aufgabentyp oder Reihenfolge der Aufgaben (alle task_ids genau
    einmal) eines Werkstatt-Kurses.
    """
    body: dict[str, Any] = {}
    if titel is not None:
        body["title"] = titel
    if standard_typ is not None:
        body["default_kind"] = standard_typ
    if reihenfolge is not None:
        body["reihenfolge"] = list(reihenfolge)
    if not body:
        return {"ok": False, "fehler": "Mindestens eine Aenderung angeben"}
    return _ws_patch(f"/kurse/{_seg(course_id)}", body)


def werkstatt_kurs_uebernehmen(course_id: str) -> dict[str, Any]:
    """Uebernimmt die Aufgaben eines bereits veroeffentlichten Kurses als erste Fassungen in
    die Werkstatt; veroeffentlicht nichts. Meldet Zaehler, `uebersprungen_details` und
    `lektionen_verloren`.
    """
    return _ws_post(f"/kurse/{_seg(course_id)}/uebernehmen", {})


def werkstatt_aufgabe_neu(
    course_id: str, kapitel: str, kurzfassung: str, position: int | None = None
) -> dict[str, Any]:
    """Legt eine leere Aufgabe mit Kapitel und Kurzfassung im Werkstatt-Kurs an
    (`position` optional); ausformuliert wird sie mit werkstatt_aufgabe_ausformulieren.
    """
    body: dict[str, Any] = {"chapter_title": kapitel, "summary": kurzfassung}
    if position is not None:
        body["position"] = position
    return _ws_post(f"/kurse/{_seg(course_id)}/aufgaben", body)


def werkstatt_aufgabe_ausformulieren(
    task_id: str, basis: int | None, wunsch: str | None = None, typ: str | None = None
) -> dict[str, Any]:
    """Laesst eine Aufgabe aus Quellen ausformulieren und legt das Ergebnis als neue Fassung ab.
    `basis` ist die Nummer der neuesten Fassung (null, wenn es noch keine gibt); bei
    Abweichung kommt ein Konflikt mit `neueste`. Auftraege laufen im Hintergrund; mit
    werkstatt_auftrag nachsehen.
    """
    return _ws_post(
        f"/aufgaben/{_seg(task_id)}/ausformulieren",
        {"based_on": basis, "wunsch": wunsch, "kind": typ},
    )


def werkstatt_aufgabe_aendern(
    task_id: str, basis: int | None, felder: dict[str, Any], grund: str
) -> dict[str, Any]:
    """Aendert Felder einer Aufgabe und legt eine neue Fassung mit Begruendung `grund` an.
    `basis` ist die Nummer der neuesten Fassung; bei Abweichung kommt ein Konflikt mit `neueste`.
    """
    return _ws_patch(
        f"/aufgaben/{_seg(task_id)}", {"based_on": basis, "felder": dict(felder), "reason": grund}
    )


def werkstatt_aufgabe_loeschen(task_id: str) -> dict[str, Any]:
    """Loescht eine Aufgabe in der Werkstatt (ihre Fassungen bleiben nachvollziehbar)."""
    return _run(
        lambda client: client.delete(
            f"{_WERKSTATT}/aufgaben/{_seg(task_id)}", roles=ROLE_AUTHOR, extra=_AUTOR
        )
    )


def werkstatt_fassungen(task_id: str) -> dict[str, Any]:
    """Listet alle nummerierten Fassungen einer Aufgabe mit Autor, Grund und Befunden."""
    return _ws_get(f"/aufgaben/{_seg(task_id)}/fassungen")


def werkstatt_zurueckholen(task_id: str, fassung: int) -> dict[str, Any]:
    """Holt eine fruehere Fassung als neue neueste Fassung zurueck."""
    return _ws_post(f"/aufgaben/{_seg(task_id)}/zurueckholen", {"number": fassung})


def werkstatt_vorschau(task_id: str, fassung: int | None = None) -> dict[str, Any]:
    """Zeigt die Lernenden-Vorschau einer Fassung (ohne `fassung` die neueste)."""
    suffix = "" if fassung is None else f"?fassung={int(fassung)}"
    return _ws_get(f"/aufgaben/{_seg(task_id)}/vorschau{suffix}")


def werkstatt_auftrag(auftrag_id: str) -> dict[str, Any]:
    """Zeigt Zustand, Ergebnis und Fehler eines Hintergrund-Auftrags der Werkstatt."""
    return _ws_get(f"/auftraege/{_seg(auftrag_id)}")


# --- Werkstatt release: shown by the model, released only by the owner on the page ---------

TELEGRAM_SKRIPT_ENV = "LEARNING_CLAW_TELEGRAM_SKRIPT"
LOG_DIR_ENV = "LEARNING_CLAW_LOG_DIR"
_TELEGRAM_TIMEOUT_SECONDS = 20
# The release writes LearnHouse and then the practice catalog (drawn windows included); that
# outlasts the client's normal 30 s.
_FREIGEBEN_TIMEOUT_SECONDS = 120.0


def _log_ordner() -> Path:
    """Where child-process output goes: LEARNING_CLAW_LOG_DIR, else next to the claw's env file
    (its sidecar logs live there), else the temp folder."""
    ordner = os.environ.get(LOG_DIR_ENV, "").strip()
    if ordner:
        return Path(ordner)
    env_datei = os.environ.get("LEARNING_CLAW_ENV_FILE", "").strip()
    return Path(env_datei).parent if env_datei else Path(tempfile.gettempdir())


def _telegram_senden(nachricht: str) -> bool:
    """Send a message to the owner with the Telegram script, as a child process.

    The message goes on stdin, never in the command line (it carries a one-time secret);
    output goes to a log file, not a pipe. Nothing of it reaches the model."""
    skript = os.environ.get(TELEGRAM_SKRIPT_ENV, "").strip()
    if not skript or not Path(skript).is_file():
        logger.warning("%s is not set or missing; no phone link sent", TELEGRAM_SKRIPT_ENV)
        return False
    try:
        ordner = _log_ordner()
        ordner.mkdir(parents=True, exist_ok=True)
        with open(ordner / "telegram.log", "ab") as ausgabe:
            lauf = subprocess.run(
                [sys.executable, skript],
                input=nachricht.encode("utf-8"),
                stdout=ausgabe,
                stderr=ausgabe,
                timeout=_TELEGRAM_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Telegram phone link failed: %s", type(error).__name__)
        return False
    if lauf.returncode != 0:
        logger.warning("Telegram phone link failed with exit code %s", lauf.returncode)
    return lauf.returncode == 0


def _handy_link_senden(geheimnis: str, titel: str, nummer: int, blockiert: bool) -> bool:
    basis = handy_basis()
    if basis is None:
        return False
    zustand = (
        "Die Freigabe ist blockiert, die Seite zeigt die Befunde."
        if blockiert
        else "Sie geben dort mit einem Klick frei."
    )
    nachricht = (
        f"Werkstatt-Freigabe Nr. {nummer}: „{titel}“\n{zustand}\n"
        f"{basis[0]}/freigabe/{geheimnis}\n"
        "Der Link gilt 10 Minuten und nur einmal."
    )
    return _telegram_senden(nachricht)


def werkstatt_freigabe_anzeigen(course_id: str) -> dict[str, Any]:
    """Bereitet die Freigabe eines Werkstatt-Kurses vor und zeigt, was sich fuer Lernende
    aendern wuerde: `vergleich` (neu, geaendert, textkorrektur, geloescht mit Kurzfassung und
    Fassung alt/neu; `unveraendert` als Zahl; `offline` und `zusammengefuehrt`: Lektionen und
    Aufgaben, die danach nicht mehr online sind), `befunde` (mit `blockierend`), `blockiert` und
    `nichts_zu_veroeffentlichen`. Oeffnet die Freigabeseite im Browser dieses Rechners
    (`link_geoeffnet`) und schickt der Inhaberin, wenn eingerichtet, den Link aufs Handy
    (`handy_link_gesendet`). Freigeben kann nur die Inhaberin dort mit einem Klick, einmal und
    10 Minuten lang; es gibt kein Werkzeug dafuer. Nie behaupten, der Kurs sei freigegeben.
    """

    def act(client: LearningClient) -> dict[str, Any]:
        data = client.post(
            f"{_WERKSTATT}/kurse/{_seg(course_id)}/freigabe", {}, roles=ROLE_AUTHOR, extra=_AUTOR
        )
        titel = str(data.get("titel") or course_id)
        inhalt = {
            "art": WERKSTATT,
            "course_id": course_id,
            "titel": titel,
            "freigabe_id": str(data["freigabe_id"]),
            "nummer": int(data["nummer"]),
            "learnhouse_revision": int(data["learnhouse_revision"]),
            "vergleich": data["vergleich"],
            "befunde": list(data.get("befunde") or []),
            "blockiert": bool(data["blockiert"]),
            "nichts_zu_veroeffentlichen": bool(data["nichts_zu_veroeffentlichen"]),
        }
        anzeige = _freigaben.merken(
            inhalt["freigabe_id"], inhalt["nummer"], inhalt["learnhouse_revision"], inhalt,
            art=WERKSTATT,
        )
        # Like veroeffentlichung_anzeigen: the secret goes only to the local browser and, if
        # configured, to the owner's phone; the model gets neither the link nor the secret.
        geoeffnet = _browser_oeffnen(freigabe_link(anzeige.geheimnis))
        gesendet = _handy_link_senden(
            anzeige.geheimnis, titel, inhalt["nummer"], inhalt["blockiert"]
        )
        return {
            key: inhalt[key]
            for key in ("course_id", "titel", "nummer", "vergleich", "befunde", "blockiert",
                        "nichts_zu_veroeffentlichen")
        } | {"link_geoeffnet": geoeffnet, "handy_link_gesendet": gesendet}

    return _run(act)


def werkstatt_freigeben(geheimnis: str) -> dict[str, Any]:
    """Release a Werkstatt state after the owner pressed the button (not a model tool).

    The approval is used up even if the release then fails; the API refuses a release whose
    LearnHouse course moved since it was prepared."""

    def act(client: LearningClient) -> dict[str, Any]:
        try:
            anzeige = _freigaben.einloesen(geheimnis, art=WERKSTATT)
        except FreigabeAbgelehnt as error:
            raise LearningRejected("freigabe_abgelehnt", error.grund, 0) from error
        ref = anzeige.approval_ref
        data = client.post(
            f"{_WERKSTATT}/freigaben/{_seg(anzeige.job_id)}/freigeben",
            {"approval_ref": ref},
            roles=ROLE_PUBLISHER,
            idempotency_key_value=idempotency_key(
                "werkstatt_freigeben", {"freigabe_id": anzeige.job_id, "approval_ref": ref}
            ),
            timeout=_FREIGEBEN_TIMEOUT_SECONDS,
        )
        ergebnis: dict[str, Any] = {
            "freigabe_id": data.get("freigabe_id", anzeige.job_id),
            "titel": anzeige.inhalt.get("titel"),
            "learnhouse_revision": data["learnhouse_revision"],
        }
        for key in ("aktualisiert", "neu", "unveroeffentlicht", "katalog", "katalog_fehler"):
            if key in data:
                ergebnis[key] = data[key]
        return ergebnis

    return _run(act)
