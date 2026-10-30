"""Local drawing page: the person draws a canvas task here and submits with a button.

``GET /zeichnen/<secret>`` shows the task and a drawing surface; ``POST`` (JSON: PNG and
strokes) saves and submits once and answers with the evaluation. Same guards as the
approval page: loopback client and Host, the page's own Origin on POST, a per-response
script nonce and no foreign sources.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import secrets
from uuid import uuid4
from collections.abc import Awaitable, Callable
from typing import Any

from . import werkzeuge
from spaces.learning.services.zeichnen.flaeche import SKRIPT, flaeche_html, kriterien_anzeige, seite_html

from .zeichnung import ZeichnungAbgelehnt, objekte_aus_strichen

PREFIX = "/zeichnen/"
_SECRET = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}
_MAX_BODY = 25_000_000
_HINT_SUFFIX = "/hinweis"

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]


def _headers(nonce: str, media: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", media),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"same-origin"),
        (b"x-content-type-options", b"nosniff"),
        (
            b"content-security-policy",
            (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                "img-src data:; connect-src 'self'; form-action 'none'; frame-ancestors 'none'"
            ).encode(),
        ),
    ]


class ZeichenSeite:
    def __init__(self, port: int) -> None:
        self._hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        self._origins = {f"http://127.0.0.1:{port}".encode(), f"http://localhost:{port}".encode()}

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        headers = dict((k.lower(), v) for k, v in scope.get("headers", []))
        client = (scope.get("client") or ("", 0))[0]
        if client not in _LOOPBACK_CLIENTS or headers.get(b"host") not in self._hosts:
            await _seite(send, 403, "Nicht erlaubt", "<p>Diese Seite ist nur lokal erreichbar.</p>")
            return
        geheimnis = scope["path"][len(PREFIX):]
        hinweis = geheimnis.endswith(_HINT_SUFFIX)
        if hinweis:
            geheimnis = geheimnis.removesuffix(_HINT_SUFFIX)
        if not _SECRET.match(geheimnis):
            await _seite(send, 404, "Nicht gefunden", "<p>Diese Zeichenaufgabe gibt es nicht.</p>")
            return
        if hinweis:
            if scope["method"] != "POST" or headers.get(b"origin") not in self._origins:
                await _json(send, 403, {"ok": False, "fehler": "Hinweis nur von dieser Seite aus."})
                return
            await _koerper(receive)
            ergebnis = await asyncio.to_thread(_hinweis, geheimnis)
            await _json(send, 200 if ergebnis.get("ok") else 409, ergebnis)
            return
        if scope["method"] == "GET":
            try:
                zeichnung = werkzeuge._zeichnungen.holen(geheimnis)
            except ZeichnungAbgelehnt as error:
                await _seite(send, 410, "Nicht mehr gültig", f"<p>{html.escape(error.grund)}</p>")
                return
            await _seite(send, 200, "Zeichenaufgabe", flaeche_html(zeichnung.aufgabe, mathe=zeichnung.mathe), skript=True)
        elif scope["method"] == "POST":
            if headers.get(b"origin") not in self._origins:
                await _json(send, 403, {"ok": False, "fehler": "Abgabe nur von dieser Seite aus."})
                return
            daten = await _koerper(receive)
            if not isinstance(daten, dict):
                await _json(send, 400, {"ok": False, "fehler": "Die Abgabe ist ungültig."})
                return
            png = daten.get("png")
            if not isinstance(png, str) or not png.startswith("data:image/png;base64,"):
                await _json(send, 400, {"ok": False, "fehler": "Das Bild fehlt."})
                return
            # Saving, submitting and the model evaluation take a while; keep the loop free.
            ergebnis = await asyncio.to_thread(
                _abgeben,
                geheimnis,
                png.removeprefix("data:image/png;base64,"),
                daten.get("striche"),
            )
            await _json(send, 200 if ergebnis.get("ok") else 409, ergebnis)
        else:
            await _seite(send, 405, "Nicht erlaubt", "<p>Nur Zeichnen und Abgeben.</p>")


_SKRIPT = SKRIPT


async def _koerper(receive: Receive) -> object:
    data = b""
    while True:
        message = await receive()
        data += message.get("body", b"")
        if len(data) > _MAX_BODY:
            return None
        if not message.get("more_body"):
            break
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


async def _seite(send: Send, status: int, titel: str, inhalt: str, *, skript: bool = False) -> None:
    nonce = secrets.token_urlsafe(16)
    seite = seite_html(titel, inhalt, nonce=nonce, skript=skript)
    await send({"type": "http.response.start", "status": status,
                "headers": [*_headers(nonce, b"text/html; charset=utf-8"),
                            (b"content-length", str(len(seite)).encode())]})
    await send({"type": "http.response.body", "body": seite})


async def _json(send: Send, status: int, wert: dict[str, Any]) -> None:
    body = json.dumps(wert, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [*_headers(secrets.token_urlsafe(8), b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _abgeben(geheimnis: str, png_base64: str, striche: object) -> dict[str, Any]:
    """Save and submit the person's drawing. Not a claw tool: only this page calls it,
    after the person pressed the button.
    """
    try:
        zeichnung = werkzeuge._zeichnungen.holen(geheimnis)
        objekte = objekte_aus_strichen(striche)
    except ZeichnungAbgelehnt as error:
        return {"ok": False, "fehler": error.grund}
    if not objekte:
        return {"ok": False, "fehler": "Die Zeichenfläche ist leer."}
    sid, ref = zeichnung.session_id, zeichnung.session_ref
    offen = werkzeuge._mcp(
        "learning.canvas.open",
        "learning_canvas_open",
        session_id=sid,
        payload={"activity_id": zeichnung.activity_id, "session_ref": ref},
    )
    if not offen.get("ok"):
        return offen
    canvas = offen["daten"]["ergebnis"]["canvas"]
    revision = int(canvas["revision"])
    dokument = {**canvas["document"], "revision": revision, "objects": objekte}
    gespeichert = werkzeuge._mcp(
        "learning.canvas.save",
        "learning_canvas_save",
        write=True,
        session_id=sid,
        expected_revision=revision,
        payload={"session_ref": ref, "document": dokument, "snapshot_png_base64": png_base64},
    )
    if not gespeichert.get("ok"):
        return gespeichert
    stand = werkzeuge.fortschritt(sid)
    if not stand.get("ok"):
        return stand
    sitzungsrevision = stand["daten"]["ergebnis"]["session"]["session_revision"]
    eingereicht = werkzeuge._mcp(
        "learning.canvas.submit",
        "learning_canvas_submit",
        model=True,
        write=True,
        session_id=sid,
        expected_revision=revision + 1,
        confirmation={"confirmed": True, "approval_ref": f"zeichnung-{secrets.token_hex(8)}"},
        payload={
            "session_ref": ref,
            "submission_id": str(uuid4()),
            "adaptive_session_revision": sitzungsrevision,
        },
    )
    if not eingereicht.get("ok"):
        return eingereicht
    werkzeuge._zeichnungen.verbrauchen(geheimnis)
    bewertung = eingereicht["daten"]["ergebnis"].get("evaluation") or {}
    return {
        "ok": True,
        "daten": {
            "punkte": bewertung.get("score"),
            "angenommen": bewertung.get("accepted"),
            "sicherheit": bewertung.get("confidence"),
            # An unsure or unsupported AI decision goes to human review; say so.
            "hinweis_bewertung": None
            if bewertung.get("accepted")
            else "Die KI war sich bei dieser Bewertung nicht sicher genug. Ein Mensch prüft "
            "sie noch; bis dahin wird sie Ihrem Lernstand nicht angerechnet.",
            "kriterien": kriterien_anzeige(bewertung, eingereicht["daten"]["ergebnis"], mathe=zeichnung.mathe),
            "evaluation_id": eingereicht["daten"]["ergebnis"].get("evaluation_id"),
        },
    }


def _hinweis(geheimnis: str) -> dict[str, Any]:
    """The task's section hint (no model call, never the answer); training only."""
    try:
        zeichnung = werkzeuge._zeichnungen.holen(geheimnis)
    except ZeichnungAbgelehnt as error:
        return {"ok": False, "fehler": error.grund}
    antwort = werkzeuge.hinweis(zeichnung.session_id, zeichnung.activity_id)
    if not antwort.get("ok"):
        fehler = str(antwort.get("fehler") or "")
        if "exam" in fehler:
            fehler = "Im Prüfungsmodus gibt es keine Hinweise."
        return {"ok": False, "fehler": fehler or "Kein Hinweis verfügbar."}
    text = (antwort.get("daten") or {}).get("ergebnis", {}).get("hint")
    if not isinstance(text, str) or not text:
        return {"ok": False, "fehler": "Kein Hinweis verfügbar."}
    return {"ok": True, "hinweis": text}
