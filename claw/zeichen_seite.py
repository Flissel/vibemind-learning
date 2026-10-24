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
                await _seite(send, 410, "Nicht mehr gueltig", f"<p>{html.escape(error.grund)}</p>")
                return
            await _seite(send, 200, "Zeichenaufgabe", _flaeche(zeichnung.aufgabe), skript=True)
        elif scope["method"] == "POST":
            if headers.get(b"origin") not in self._origins:
                await _json(send, 403, {"ok": False, "fehler": "Abgabe nur von dieser Seite aus."})
                return
            daten = await _koerper(receive)
            if not isinstance(daten, dict):
                await _json(send, 400, {"ok": False, "fehler": "Die Abgabe ist ungueltig."})
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


def _flaeche(aufgabe: str) -> str:
    farben = "".join(
        f'<button type="button" class="farbe" data-farbe="{f}" style="background:{f}"></button>'
        for f in ("#111111", "#d22", "#16c", "#1a7f37", "#e8a400")
    )
    return (
        f'<p class="aufgabe">{html.escape(aufgabe) or "Zeichne deine Loesung."}</p>'
        f'<div class="leiste">{farben}'
        '<label>Staerke <input id="staerke" type="range" min="2" max="24" value="5"></label>'
        '<button type="button" id="radierer">Radierer</button>'
        '<button type="button" id="zurueck">Rueckgaengig</button>'
        '<button type="button" id="leeren">Leeren</button>'
        '<button type="button" id="hinweis">Hinweis</button></div>'
        '<p id="hinweistext" class="hinweistext"></p>'
        '<canvas id="flaeche" width="1000" height="620"></canvas>'
        '<p><button type="button" id="abgeben" class="abgeben">Abgeben</button> '
        '<span id="status"></span></p><div id="ergebnis"></div>'
    )


_SKRIPT = r"""
const c = document.getElementById('flaeche'), g = c.getContext('2d');
const striche = []; let aktiv = null, farbe = '#111111', radierer = false;
function weiss(){ g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height); }
function zeichne(s){ if (s.punkte.length < 1) return; g.strokeStyle = s.farbe; g.lineWidth = s.breite;
  g.lineCap = 'round'; g.lineJoin = 'round'; g.beginPath(); g.moveTo(...s.punkte[0]);
  for (const p of s.punkte) g.lineTo(...p); g.stroke(); }
function neu(){ weiss(); striche.forEach(zeichne); }
function punkt(e){ const r = c.getBoundingClientRect();
  return [Math.round((e.clientX - r.left) * c.width / r.width), Math.round((e.clientY - r.top) * c.height / r.height)]; }
c.addEventListener('pointerdown', e => { c.setPointerCapture(e.pointerId);
  aktiv = {farbe: radierer ? '#ffffff' : farbe, breite: +document.getElementById('staerke').value * (radierer ? 3 : 1), punkte: [punkt(e)]};
  striche.push(aktiv); });
c.addEventListener('pointermove', e => { if (!aktiv) return; aktiv.punkte.push(punkt(e)); zeichne(aktiv); });
['pointerup','pointercancel'].forEach(t => c.addEventListener(t, () => { aktiv = null; }));
document.querySelectorAll('.farbe').forEach(b => b.onclick = () => { farbe = b.dataset.farbe; radierer = false; });
document.getElementById('radierer').onclick = () => { radierer = !radierer; };
document.getElementById('zurueck').onclick = () => { striche.pop(); neu(); };
document.getElementById('leeren').onclick = () => { if (confirm('Alles loeschen?')) { striche.length = 0; neu(); } };
const status = document.getElementById('status'), knopf = document.getElementById('abgeben');
knopf.onclick = async () => {
  const sichtbar = striche.filter(s => s.farbe !== '#ffffff');
  if (!sichtbar.length) { status.textContent = 'Die Zeichenflaeche ist noch leer.'; return; }
  if (!confirm('Zeichnung jetzt abgeben? Danach ist sie nicht mehr aenderbar.')) return;
  knopf.disabled = true; status.textContent = 'Wird abgegeben und bewertet, das dauert bis zu einige Minuten ...';
  try {
    const antwort = await fetch(location.pathname, {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({png: c.toDataURL('image/png'), striche: sichtbar.map(s => ({punkte: s.punkte, farbe: s.farbe, breite: s.breite}))})});
    const d = await antwort.json(); const box = document.getElementById('ergebnis');
    if (!d.ok) { status.textContent = 'Nicht abgegeben: ' + (d.fehler || 'unbekannter Fehler'); knopf.disabled = false; return; }
    status.textContent = 'Abgegeben.';
    const h = document.createElement('h2'); h.textContent = (d.daten.angenommen === false ? 'Vorlaeufiges Ergebnis: ' : 'Ergebnis: ') + Math.round((d.daten.punkte || 0) * 100) + ' %'; box.appendChild(h);
    if (d.daten.hinweis_bewertung) { const n = document.createElement('p'); n.className = 'hinweistext'; n.textContent = d.daten.hinweis_bewertung; box.appendChild(n); }
    for (const k of d.daten.kriterien || []) { const p = document.createElement('p');
      p.textContent = (k.kriterium || '') + ' (' + Math.round((k.punkte || 0) * 100) + ' %): ' + (k.rueckmeldung || ''); box.appendChild(p); }
  } catch (err) { status.textContent = 'Abgabe fehlgeschlagen: ' + err; knopf.disabled = false; }
};
const hinweisKnopf = document.getElementById('hinweis'), hinweisText = document.getElementById('hinweistext');
hinweisKnopf.onclick = async () => {
  hinweisKnopf.disabled = true; hinweisText.textContent = 'Hinweis wird geholt ...';
  try {
    const d = await (await fetch(location.pathname + '/hinweis', {method: 'POST', headers: {'content-type': 'application/json'}, body: '{}'})).json();
    hinweisText.textContent = d.ok ? 'Hinweis: ' + d.hinweis : 'Kein Hinweis: ' + (d.fehler || 'nicht verfuegbar');
  } catch (err) { hinweisText.textContent = 'Kein Hinweis: ' + err; }
  hinweisKnopf.disabled = false;
};
weiss();
"""


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
    script = f'<script nonce="{nonce}">{_SKRIPT}</script>' if skript else ""
    seite = (
        "<!doctype html><html lang=\"de\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(titel)}</title><style>"
        "body{font-family:sans-serif;max-width:64rem;margin:1.5rem auto;padding:0 1rem}"
        ".aufgabe{font-size:1.15rem}.leiste{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin:.5rem 0}"
        ".farbe{width:2rem;height:2rem;border:2px solid #888;border-radius:50%;cursor:pointer}"
        "canvas{border:1px solid #999;width:100%;height:auto;touch-action:none;background:#fff}"
        ".abgeben{font-size:1.1rem;padding:.5rem 1.5rem}.hinweistext{background:#f4f4ee;padding:.4rem .6rem}"
        ".hinweistext:empty{display:none}</style>"
        f"<body><h1>{html.escape(titel)}</h1>{inhalt}{script}</body></html>"
    ).encode("utf-8")
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
        return {"ok": False, "fehler": "Die Zeichenflaeche ist leer."}
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
            else "Die KI war sich bei dieser Bewertung nicht sicher genug. Ein Mensch prueft "
            "sie noch; bis dahin wird sie deinem Lernstand nicht angerechnet.",
            "kriterien": [
                {"kriterium": c.get("criterion_id"), "punkte": c.get("score"),
                 "rueckmeldung": c.get("feedback")}
                for c in bewertung.get("criteria") or []
            ],
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
            fehler = "Im Pruefungsmodus gibt es keine Hinweise."
        return {"ok": False, "fehler": fehler or "Kein Hinweis verfuegbar."}
    text = (antwort.get("daten") or {}).get("ergebnis", {}).get("hint")
    if not isinstance(text, str) or not text:
        return {"ok": False, "fehler": "Kein Hinweis verfuegbar."}
    return {"ok": True, "hinweis": text}
