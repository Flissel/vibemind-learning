"""Drawing tasks in the LearnHouse player: ``/zeichnen/<token>`` in learning-api.

learning-mcp issues the signed link when the person opens a canvas task; the page draws,
gives the section hint and submits once, when the person presses the button. Every call
goes to learning-mcp as the person of the link, so ownership and revisions are checked
there exactly as for the claw. Embeddable only in the LearnHouse origins.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from spaces.learning.claw.zeichnung import ZeichnungAbgelehnt, objekte_aus_strichen
from spaces.learning.services.zeichnen.flaeche import flaeche_html, seite_html
from spaces.learning.services.zeichnen.link import DrawingGrant, read_drawing_token

MCP_URL_ENV = "LEARNING_MCP_URL"
_TIMEOUT_S = 660.0
_MAX_PNG_CHARS = 25_000_000

router = APIRouter()


def _headers(nonce: str | None = None) -> dict[str, str]:
    ancestors = os.environ.get("LEARNING_EMBED_ANCESTORS", "http://127.0.0.1:13000 http://localhost:13000").strip()
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            f"default-src 'none'; script-src {script}; style-src 'unsafe-inline'; img-src data:; "
            f"connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'self' {ancestors}"
        ),
    }


def _page(status: int, title: str, body: str, *, script: bool = False) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    return HTMLResponse(seite_html(title, body, nonce=nonce, skript=script), status_code=status,
                        headers=_headers(nonce))


def _json(status: int, value: dict[str, Any]) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers=_headers())


def _grant(token: str) -> DrawingGrant | None:
    try:
        return read_drawing_token(token)
    except (LookupError, RuntimeError):
        return None


def _same_origin(request: Request) -> bool:
    """POSTs only from the drawing page itself (its public origin)."""
    public = urlsplit(os.environ.get("LEARNING_PUBLIC_API_URL", ""))
    expected = f"{public.scheme}://{public.netloc}" if public.netloc else ""
    return bool(expected) and request.headers.get("origin") == expected


def _mcp(tool: str, grant: DrawingGrant, *, write: bool = False, **fields: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_type": tool.replace("_", "."),  # learning_canvas_open -> learning.canvas.open
        "actor": {"actor_id": grant.actor_id, "actor_type": "local_user"},
        "session_id": grant.session_id,
        "payload": {},
        **fields,
    }
    if write:
        event["idempotency_key"] = "zeichnen-" + hashlib.sha256(
            (tool + json.dumps(event, sort_keys=True)).encode()
        ).hexdigest()[:40]
    event = {"invocation_id": str(uuid4()), "correlation_id": str(uuid4()), **event}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": event}}
    try:
        response = httpx.post(os.environ.get(MCP_URL_ENV, "http://learning-mcp:8091/mcp"), json=body,
                              timeout=_TIMEOUT_S)
        message = response.json()
        result = message["result"]
        payload = json.loads(result["content"][0]["text"])
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return {"ok": False, "fehler": "Learning ist gerade nicht erreichbar."}
    if result.get("isError"):
        error = payload.get("error")
        detail = error.get("message") if isinstance(error, dict) else str(error or "abgelehnt")
        return {"ok": False, "fehler": str(detail)[:300]}
    return {"ok": True, "ergebnis": payload.get("result") or {}}


def _task(grant: DrawingGrant) -> dict[str, Any]:
    stand = _mcp("learning_progress_show", grant)
    task = ((stand.get("ergebnis") or {}).get("session") or {}).get("task") or {}
    return task if stand["ok"] and isinstance(task, dict) else {}


def _task_prompt(grant: DrawingGrant) -> str:
    return str(_task(grant).get("prompt") or "")


@router.get("/zeichnen/{token}", response_class=HTMLResponse)
def page(token: str) -> HTMLResponse:
    grant = _grant(token)
    if grant is None:
        return _page(404, "Nicht gefunden", "<p>Dieser Zeichenlink ist ungueltig oder abgelaufen.</p>")
    task = _task(grant)
    presentation = task.get("presentation")
    mathe = isinstance(presentation, dict) and presentation.get("math") is True
    return _page(200, "Zeichenaufgabe", flaeche_html(str(task.get("prompt") or ""), mathe=mathe), script=True)


@router.post("/zeichnen/{token}/hinweis")
def hint(token: str, request: Request) -> JSONResponse:
    grant = _grant(token)
    if grant is None:
        return _json(404, {"ok": False, "fehler": "Dieser Zeichenlink ist ungueltig oder abgelaufen."})
    if not _same_origin(request):
        return _json(403, {"ok": False, "fehler": "Hinweis nur von dieser Seite aus."})
    answer = _mcp("learning_hint_request", grant, payload={"task_id": grant.activity_id})
    if not answer["ok"]:
        fehler = answer["fehler"]
        return _json(409, {"ok": False, "fehler": "Im Pruefungsmodus gibt es keine Hinweise." if "exam" in fehler else fehler})
    text = answer["ergebnis"].get("hint")
    if not isinstance(text, str) or not text:
        return _json(409, {"ok": False, "fehler": "Kein Hinweis verfuegbar."})
    return _json(200, {"ok": True, "hinweis": text})


@router.post("/zeichnen/{token}")
def submit(token: str, request: Request, payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
    grant = _grant(token)
    if grant is None:
        return _json(404, {"ok": False, "fehler": "Dieser Zeichenlink ist ungueltig oder abgelaufen."})
    if not _same_origin(request):
        return _json(403, {"ok": False, "fehler": "Abgabe nur von dieser Seite aus."})
    png = payload.get("png")
    if not isinstance(png, str) or not png.startswith("data:image/png;base64,") or len(png) > _MAX_PNG_CHARS:
        return _json(400, {"ok": False, "fehler": "Das Bild fehlt."})
    try:
        objekte = objekte_aus_strichen(payload.get("striche"))
    except ZeichnungAbgelehnt as error:
        return _json(400, {"ok": False, "fehler": error.grund})
    if not objekte:
        return _json(400, {"ok": False, "fehler": "Die Zeichenflaeche ist leer."})
    ergebnis = _abgeben(grant, png.removeprefix("data:image/png;base64,"), objekte)
    return _json(200 if ergebnis.get("ok") else 409, ergebnis)


def _abgeben(grant: DrawingGrant, png_base64: str, objekte: list[dict[str, Any]]) -> dict[str, Any]:
    ref = grant.session_ref
    offen = _mcp("learning_canvas_open", grant, payload={"activity_id": grant.activity_id, "session_ref": ref})
    if not offen["ok"]:
        return offen
    canvas = offen["ergebnis"]["canvas"]
    revision = int(canvas["revision"])
    dokument = {**canvas["document"], "revision": revision, "objects": objekte}
    gespeichert = _mcp("learning_canvas_save", grant, write=True, expected_revision=revision,
                       payload={"session_ref": ref, "document": dokument, "snapshot_png_base64": png_base64})
    if not gespeichert["ok"]:
        return gespeichert
    stand = _mcp("learning_progress_show", grant)
    if not stand["ok"]:
        return stand
    eingereicht = _mcp(
        "learning_canvas_submit", grant, write=True, expected_revision=revision + 1,
        # The person pressed the submit button on this page; that is the confirmation.
        confirmation={"confirmed": True, "approval_ref": f"zeichnung-{secrets.token_hex(8)}"},
        payload={
            "session_ref": ref,
            "submission_id": str(uuid4()),
            "adaptive_session_revision": stand["ergebnis"]["session"]["session_revision"],
        },
    )
    if not eingereicht["ok"]:
        return eingereicht
    bewertung = eingereicht["ergebnis"].get("evaluation") or {}
    return {
        "ok": True,
        "daten": {
            "punkte": bewertung.get("score"),
            "angenommen": bewertung.get("accepted"),
            "hinweis_bewertung": None
            if bewertung.get("accepted")
            else "Die KI war sich bei dieser Bewertung nicht sicher genug. Ein Mensch prueft "
            "sie noch; bis dahin wird sie deinem Lernstand nicht angerechnet.",
            "kriterien": [
                {"kriterium": c.get("criterion_id"), "punkte": c.get("score"), "rueckmeldung": c.get("feedback")}
                for c in bewertung.get("criteria") or []
            ],
        },
    }
