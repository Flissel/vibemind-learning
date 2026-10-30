"""Uebersichtsseite der Kursbewertungen: nur Lesen, erreichbar ueber einen signierten Link.

``GET /bewertungen/<token>``. Gleiche Regeln wie die Freigabeseite fuer Loopback-Client und
Host (auch der Host der Handy-Basis); ohne gueltigen Token gibt es 403 und keine Daten.
Keine actor_id und keine Namen: Learning liefert sie nicht aus.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from typing import Any

from .bewertungen import PREFIX, token_gueltig
from .formeln import KOPF, csp
from .freigabe import handy_basis

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]

_EMOJI = {"gut": "😃", "mittel": "🙂", "schlecht": "😕"}
_HEADERS = [
    (b"content-type", b"text/html; charset=utf-8"),
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"same-origin"),
    (b"x-content-type-options", b"nosniff"),
    (b"content-security-policy", csp()),
]


class BewertungenSeite:
    def __init__(self, port: int) -> None:
        self._hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        basis = handy_basis()
        if basis is not None:
            self._hosts.add(basis[1].encode())

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        # Imported here: werkzeuge and freigabe_seite import each other's neighbours.
        from . import werkzeuge
        from .freigabe_seite import _LOOPBACK_CLIENTS

        headers = dict((k.lower(), v) for k, v in scope.get("headers", []))
        client = (scope.get("client") or ("", 0))[0]
        if client not in _LOOPBACK_CLIENTS or headers.get(b"host", b"").lower() not in self._hosts:
            await _antwort(send, 403, "Nicht erlaubt", "<p>Diese Seite ist nur lokal erreichbar.</p>")
            return
        if scope["method"] != "GET":
            await _antwort(send, 405, "Nicht erlaubt", "<p>Nur Anzeigen.</p>")
            return
        if not token_gueltig(scope["path"][len(PREFIX):]):
            await _antwort(send, 403, "Nicht erlaubt", "<p>Dieser Link ist abgelaufen oder ungültig.</p>")
            return
        try:
            with werkzeuge._client() as api:
                daten = api.get("/kursbewertungen/uebersicht", roles=werkzeuge.ROLE_AUTHOR)
            kurse = daten.get("kurse") or []
        except Exception:  # noqa: BLE001 - nothing of the error reaches the page
            await _antwort(
                send, 503, "Nicht erreichbar", "<p>Die Bewertungen sind gerade nicht erreichbar.</p>"
            )
            return
        await _antwort(send, 200, "Kursbewertungen", _kurse(kurse))


def _e(wert: Any) -> str:
    return html.escape(str(wert))


def _kurse(kurse: list[dict[str, Any]]) -> str:
    if not kurse:
        return "<p>Es gibt noch keine Bewertungen.</p>"
    return "".join(_kurs(k) for k in kurse)


def _kurs(k: dict[str, Any]) -> str:
    v = k.get("verteilung") or {}
    summe = sum(int(v.get(s) or 0) for s in _EMOJI)
    verteilung = " · ".join(f"{int(v.get(s) or 0)} {e}" for s, e in _EMOJI.items())
    teile = [
        '<section style="border:1px solid #999;border-radius:6px;padding:1rem;margin:1rem 0">',
        f"<h2>{_e(k.get('kurs_titel') or 'Ohne Titel')}</h2>",
        f"<p>{summe} Bewertungen: {verteilung}</p>",
    ]
    top = k.get("stichworte_top") or []
    if top:
        teile.append(
            "<p><strong>Häufigste Stichworte:</strong> "
            + ", ".join(f"{_e(w)} ({_e(n)})" for w, n in top)
            + "</p>"
        )
    teile.append("<ul>")
    for z in k.get("einzeln") or []:
        worte = ", ".join(_e(w) for w in z.get("stichworte") or []) or "ohne Stichworte"
        teile.append(
            f"<li>{_EMOJI.get(z.get('smiley'), '')} {worte} "
            f"<small>({_e(str(z.get('datum'))[:10])})</small></li>"
        )
    teile.append("</ul></section>")
    return "".join(teile)


async def _antwort(send: Send, status: int, titel: str, text: str) -> None:
    seite = (
        "<!doctype html><html lang=\"de\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(titel)}</title>{KOPF}<body style=\"font-family:sans-serif;max-width:40rem;"
        f"margin:2rem auto;padding:0 1rem\"><h1>{html.escape(titel)}</h1>{text}</body></html>"
    ).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [*_HEADERS, (b"content-length", str(len(seite)).encode())]})
    await send({"type": "http.response.body", "body": seite})
