"""Local review page: the person decides held AI evaluations here, outside the model.

``pruefungen_anzeigen`` records the shown reviews under a random secret and opens
``/pruefung/<secret>`` in the browser on this machine. ``GET`` lists them with task,
answer, drawing and the AI result; a ``POST`` confirms the AI score or sets new points
for one review, once. Same guards as the approval page: loopback client and Host,
this page's own Origin on every POST.
"""

from __future__ import annotations

import html
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .freigabe import FreigabeAbgelehnt

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]

PREFIX = "/pruefung/"
_SECRET = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_HEADERS = [
    (b"content-type", b"text/html; charset=utf-8"),
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"same-origin"),
    (b"x-content-type-options", b"nosniff"),
    (
        b"content-security-policy",
        b"default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
        b"frame-ancestors 'none'",
    ),
]
_GRUENDE = {
    "low_confidence": "Die KI war sich unsicher",
    "feedback_not_grounded": "Die Begruendung der KI stuetzt sich nicht auf die Quelle",
    "evaluator_schema_invalid": "Die KI-Antwort war unvollstaendig",
}


@dataclass(frozen=True)
class PruefAnzeige:
    geheimnis: str
    ausgestellt: float
    eintraege: dict[str, dict[str, Any]] = field(default_factory=dict)


class PruefungStore:
    """Shown reviews with a TTL; each review in a display can be decided once."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl_seconds: float = 1800) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._anzeigen: dict[str, PruefAnzeige] = {}
        self._lock = threading.Lock()

    def merken(self, eintraege: list[dict[str, Any]]) -> PruefAnzeige:
        with self._lock:
            now = self._clock()
            self._aufraeumen(now)
            anzeige = PruefAnzeige(
                secrets.token_urlsafe(24), now, {e["review_id"]: dict(e) for e in eintraege}
            )
            self._anzeigen[anzeige.geheimnis] = anzeige
        return anzeige

    def holen(self, geheimnis: str) -> PruefAnzeige:
        with self._lock:
            self._aufraeumen(self._clock())
            anzeige = self._anzeigen.get(geheimnis)
        if anzeige is None:
            raise FreigabeAbgelehnt("Diese Pruefseite gibt es nicht oder sie ist abgelaufen")
        return anzeige

    def einloesen(self, geheimnis: str, review_id: str) -> dict[str, Any]:
        """Take one shown review out of the display; a second decision is refused."""
        with self._lock:
            self._aufraeumen(self._clock())
            anzeige = self._anzeigen.get(geheimnis)
            eintrag = anzeige.eintraege.pop(review_id, None) if anzeige else None
        if anzeige is None:
            raise FreigabeAbgelehnt("Diese Pruefseite gibt es nicht oder sie ist abgelaufen")
        if eintrag is None:
            raise FreigabeAbgelehnt("Diese Pruefung ist schon entschieden oder gehoert nicht hierher")
        return eintrag

    def _aufraeumen(self, now: float) -> None:
        self._anzeigen = {
            key: value for key, value in self._anzeigen.items() if now - value.ausgestellt < self._ttl
        }


class PruefungSeite:
    def __init__(self, port: int) -> None:
        self._hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        self._origins = {f"http://127.0.0.1:{port}".encode(), f"http://localhost:{port}".encode()}

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        # Imported here: werkzeuge imports this module, and freigabe_seite imports werkzeuge.
        from . import werkzeuge
        from .freigabe_seite import _LOOPBACK_CLIENTS, _koerper

        headers = dict((k.lower(), v) for k, v in scope.get("headers", []))
        client = (scope.get("client") or ("", 0))[0]
        if client not in _LOOPBACK_CLIENTS or headers.get(b"host") not in self._hosts:
            await _antwort(send, 403, "Nicht erlaubt", "Diese Seite ist nur lokal erreichbar.")
            return
        geheimnis = scope["path"][len(PREFIX):]
        if not _SECRET.match(geheimnis):
            await _antwort(send, 404, "Nicht gefunden", "Diese Pruefseite gibt es nicht.")
            return
        if scope["method"] == "GET":
            try:
                anzeige = werkzeuge._pruefungen.holen(geheimnis)
            except FreigabeAbgelehnt as error:
                await _antwort(send, 410, "Pruefseite nicht mehr gueltig", error.grund)
                return
            await _antwort(send, 200, "KI-Bewertungen pruefen", _liste(anzeige))
        elif scope["method"] == "POST":
            if headers.get(b"origin") not in self._origins:
                await _antwort(send, 403, "Nicht erlaubt", "Entscheiden nur von dieser Seite aus.")
                return
            form = await _koerper(receive)
            ergebnis = werkzeuge.pruefung_entscheiden(
                geheimnis,
                _feld(form, "review_id"),
                _feld(form, "entscheidung"),
                _feld(form, "punkte"),
                _feld(form, "notiz"),
            )
            zurueck = f'<p><a href="{html.escape(PREFIX + geheimnis)}">Zurueck zur Liste</a></p>'
            if ergebnis.get("ok"):
                daten = ergebnis["daten"]
                await _antwort(
                    send, 200, "Entschieden",
                    f"<p>Gewertet mit {round(float(daten['punkte']) * 100)} %. Der Lernstand ist "
                    f"{'angerechnet' if daten.get('lernstand_angerechnet') else 'unveraendert'}.</p>"
                    + zurueck,
                )
            else:
                await _antwort(
                    send, 409, "Nicht entschieden",
                    f"<p>{html.escape(str(ergebnis.get('fehler')))}</p>" + zurueck,
                )
        else:
            await _antwort(send, 405, "Nicht erlaubt", "Nur Anzeigen und Entscheiden.")


def _feld(form: dict[str, list[str]], name: str) -> str:
    werte = form.get(name) or [""]
    return werte[0]


def _liste(anzeige: PruefAnzeige) -> str:
    if not anzeige.eintraege:
        return "<p>Alle angezeigten Pruefungen sind entschieden.</p>"
    return "".join(_eintrag(e) for e in anzeige.eintraege.values())


def _eintrag(e: dict[str, Any]) -> str:
    ki = round(float(e.get("ki_punkte") or 0) * 100)
    sicherheit = round(float(e.get("ki_sicherheit") or 0) * 100)
    grund = _GRUENDE.get(str(e.get("grund")), str(e.get("grund")))
    teile = [
        '<section style="border:1px solid #999;border-radius:6px;padding:1rem;margin:1rem 0">',
        f"<h2>{html.escape(str(e.get('aufgabe') or 'Aufgabe'))}</h2>",
        f"<p><em>{html.escape(grund)}</em></p>",
    ]
    if e.get("zeichnung_png_base64"):
        teile.append(
            '<img alt="Zeichnung der lernenden Person" style="max-width:100%;border:1px solid #ccc" '
            f'src="data:image/png;base64,{html.escape(str(e["zeichnung_png_base64"]))}">'
        )
    if e.get("antwort"):
        teile.append(f"<p><strong>Antwort:</strong> {html.escape(str(e['antwort']))}</p>")
    if e.get("erwartet"):
        teile.append(f"<p><strong>Erwartet:</strong> {html.escape(str(e['erwartet']))}</p>")
    kriterien = e.get("kriterien") or []
    if kriterien:
        teile.append(
            "<p><strong>Kriterien:</strong></p><ul>"
            + "".join(
                f"<li>{html.escape(str(k.get('description')))} ({html.escape(str(k.get('points')))} P.)</li>"
                for k in kriterien if isinstance(k, dict)
            )
            + "</ul>"
        )
    rid = html.escape(str(e["review_id"]))
    teile.append(
        f"<p><strong>KI:</strong> {ki} % bei {sicherheit} % Sicherheit</p>"
        '<form method="post">'
        f'<input type="hidden" name="review_id" value="{rid}">'
        '<p><button type="submit" name="entscheidung" value="confirm">KI-Bewertung bestaetigen'
        "</button></p>"
        f'<p><label>Eigene Punkte (0-100 %): <input type="number" name="punkte" min="0" max="100" '
        f'step="5" value="{ki}"></label> '
        '<button type="submit" name="entscheidung" value="override">Punkte uebernehmen</button></p>'
                # The form body is read up to 4 KiB; 500 characters fit even as encoded umlauts.
        '<p><label>Notiz (optional):<br><textarea name="notiz" maxlength="500" rows="2" '
        'cols="50"></textarea></label></p>'
        "</form></section>"
    )
    return "".join(teile)


async def _antwort(send: Send, status: int, titel: str, text: str) -> None:
    seite = (
        "<!doctype html><html lang=\"de\"><meta charset=\"utf-8\"><title>"
        f"{html.escape(titel)}</title><body style=\"font-family:sans-serif;max-width:48rem;"
        f"margin:2rem auto\"><h1>{html.escape(titel)}</h1>{text}</body></html>"
    ).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [*_HEADERS, (b"content-length", str(len(seite)).encode())]})
    await send({"type": "http.response.body", "body": seite})
