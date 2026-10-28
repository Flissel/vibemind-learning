"""Local approval page: the person publishes here, outside the model.

``GET /freigabe/<secret>`` shows what would be published; ``POST`` to the same address
publishes once. Requests must come from loopback with a loopback Host header (no DNS
rebinding), and a POST must carry this page's own Origin (no cross-site form posts).

Werkstatt releases (``art == "werkstatt"``) show the comparison with the last release and the
findings; the button is only active when nothing blocks. With ``LEARNING_CLAW_FREIGABE_BASIS``
the page also accepts exactly that https host and origin: ``tailscale serve`` forwards the
phone's request from loopback, so the client check stays as it is.
"""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

from . import werkzeuge
from .freigabe import WERKSTATT, FreigabeAbgelehnt, handy_basis

PREFIX = "/freigabe/"
_SECRET = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}
_HEADERS = [
    (b"content-type", b"text/html; charset=utf-8"),
    (b"cache-control", b"no-store"),
    # Not "no-referrer": with it Chrome sends "Origin: null" on the form post and the
    # real click was refused (24.10.). "same-origin" still keeps the link from others.
    (b"referrer-policy", b"same-origin"),
    (b"x-content-type-options", b"nosniff"),
    (
        b"content-security-policy",
        b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    ),
]

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]


class FreigabeSeite:
    def __init__(self, port: int) -> None:
        self._hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        self._origins = {f"http://127.0.0.1:{port}".encode(), f"http://localhost:{port}".encode()}
        basis = handy_basis()
        if basis is not None:
            origin, host = basis
            self._hosts.add(host.encode())
            self._origins.add(origin.encode())

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        headers = dict((k.lower(), v) for k, v in scope.get("headers", []))
        client = (scope.get("client") or ("", 0))[0]
        if client not in _LOOPBACK_CLIENTS or headers.get(b"host") not in self._hosts:
            await _antwort(send, 403, "Nicht erlaubt", "Diese Seite ist nur lokal erreichbar.")
            return
        geheimnis = scope["path"][len(PREFIX):]
        if not _SECRET.match(geheimnis):
            await _antwort(send, 404, "Nicht gefunden", "Diese Freigabe gibt es nicht.")
            return
        if scope["method"] == "GET":
            await self._zeigen(send, geheimnis)
        elif scope["method"] == "POST":
            if headers.get(b"origin") not in self._origins:
                await _antwort(send, 403, "Nicht erlaubt", "Freigabe nur von dieser Seite aus.")
                return
            await _koerper(receive)
            await self._veroeffentlichen(send, geheimnis)
        else:
            await _antwort(send, 405, "Nicht erlaubt", "Nur Anzeigen und Freigeben.")

    async def _zeigen(self, send: Send, geheimnis: str) -> None:
        try:
            anzeige = werkzeuge._freigaben.holen(geheimnis)
        except FreigabeAbgelehnt as error:
            await _antwort(send, 410, "Freigabe nicht mehr gueltig", error.grund)
            return
        if anzeige.art == WERKSTATT:
            await _antwort(
                send, 200, "Werkstatt-Stand freigeben?", _werkstatt_inhalt(anzeige.inhalt),
                knopf=_freigebbar(anzeige.inhalt), knopf_text="Jetzt freigeben",
            )
            return
        await _antwort(send, 200, "Kurs veroeffentlichen?", _inhalt(anzeige.inhalt), knopf=True)

    async def _veroeffentlichen(self, send: Send, geheimnis: str) -> None:
        try:
            anzeige = werkzeuge._freigaben.holen(geheimnis)
        except FreigabeAbgelehnt as error:
            await _antwort(send, 410, "Freigabe nicht mehr gültig", html.escape(error.grund))
            return
        if anzeige.art == WERKSTATT:
            await self._werkstatt_freigeben(send, geheimnis, anzeige.inhalt)
            return
        ergebnis = werkzeuge.freigeben(geheimnis)
        if ergebnis.get("ok"):
            daten = ergebnis["daten"]
            await _antwort(
                send, 200, "Veroeffentlicht",
                f"Der Auftrag {html.escape(str(daten.get('job_id')))} steht jetzt auf "
                f"{html.escape(str(daten.get('zustand')))}.",
            )
        else:
            await _antwort(
                send, 409, "Nicht veroeffentlicht", html.escape(str(ergebnis.get("fehler")))
            )

    async def _werkstatt_freigeben(self, send: Send, geheimnis: str, inhalt: dict[str, Any]) -> None:
        if not _freigebbar(inhalt):
            # The button is disabled; a hand-made POST is refused without asking Learning.
            await _antwort(
                send, 409, "Nicht freigegeben",
                "<p>Diese Freigabe ist blockiert oder hat nichts zu veröffentlichen.</p>",
            )
            return
        ergebnis = werkzeuge.werkstatt_freigeben(geheimnis)
        if not ergebnis.get("ok"):
            await _antwort(
                send, 409, "Nicht freigegeben", f"<p>{html.escape(str(ergebnis.get('fehler')))}</p>"
            )
            return
        daten = ergebnis["daten"]
        teile = [
            f"<p>Der Stand von „{html.escape(str(inhalt.get('titel')))}“ ist freigegeben und für "
            f"Lernende sichtbar: LearnHouse-Revision {int(daten.get('learnhouse_revision') or 0)}.</p>"
        ]
        katalog = daten.get("katalog")
        if isinstance(katalog, dict):
            teile.append(
                f"<p>Übungskatalog geschrieben: {int(katalog.get('items') or 0)} Aufgaben, "
                f"{int(katalog.get('konzepte') or 0)} Konzepte, Lernstand übernommen: "
                f"{int(katalog.get('lernstand_uebernommen') or 0)}.</p>"
            )
        elif daten.get("katalog_fehler"):
            teile.append(
                "<p>Der Übungskatalog konnte nicht geschrieben werden (Fehlerart "
                f"{html.escape(str(daten['katalog_fehler']))}). Die Freigabe gilt trotzdem; der "
                "Katalog lässt sich nachholen.</p>"
            )
        await _antwort(send, 200, "Freigegeben", "".join(teile))


def _freigebbar(inhalt: dict[str, Any]) -> bool:
    return not inhalt.get("blockiert") and not inhalt.get("nichts_zu_veroeffentlichen")


_ABSCHNITTE = (
    ("neu", "Neu"),
    ("geaendert", "Geändert (Lösung oder Aufgabe anders, gilt für Lernende als neue Aufgabe)"),
    ("textkorrektur", "Textkorrektur (Lernstand bleibt)"),
    ("geloescht", "Gelöscht"),
)


def _fassung(eintrag: dict[str, Any]) -> str:
    alt, neu = eintrag.get("fassung_alt"), eintrag.get("fassung_neu")
    if alt is None:
        return f"Fassung {int(neu or 0)}"
    if neu is None:
        return f"bisher Fassung {int(alt)}"
    return f"Fassung {int(alt)} → {int(neu)}"


def _werkstatt_inhalt(inhalt: dict[str, Any]) -> str:
    teile = [
        f"<h2>{html.escape(str(inhalt.get('titel') or 'Ohne Titel'))}</h2>",
        f"<p>Freigabe Nr. {int(inhalt.get('nummer') or 0)} gegenüber der letzten Freigabe:</p>",
    ]
    vergleich = inhalt.get("vergleich") or {}
    for key, ueberschrift in _ABSCHNITTE:
        eintraege = vergleich.get(key) or []
        if not eintraege:
            continue
        teile.append(f"<h3>{html.escape(ueberschrift)}: {len(eintraege)}</h3><ul>")
        for e in eintraege:
            teile.append(
                f"<li><strong>{html.escape(str(e.get('summary') or 'Ohne Kurzfassung'))}</strong> "
                f"({html.escape(str(e.get('kapitel') or ''))}, {_fassung(e)})</li>"
            )
        teile.append("</ul>")
    teile.append(f"<p>Unverändert: {int(vergleich.get('unveraendert') or 0)}</p>")
    befunde = inhalt.get("befunde") or []
    if befunde:
        teile.append("<h3>Befunde</h3><ul>")
        for b in befunde:
            art = "blockiert die Freigabe" if b.get("blockierend") else "Hinweis"
            teile.append(f"<li><strong>{art}:</strong> {html.escape(str(b.get('text') or b.get('code')))}</li>")
        teile.append("</ul>")
    if inhalt.get("blockiert"):
        teile.append(
            "<p><strong>Die Freigabe ist blockiert.</strong> Bitte beheben Sie die blockierenden "
            "Befunde in der Werkstatt und lassen Sie die Freigabe dann erneut anzeigen.</p>"
        )
    elif inhalt.get("nichts_zu_veroeffentlichen"):
        teile.append("<p>Seit der letzten Freigabe hat sich nichts geändert.</p>")
    return "".join(teile)


def _inhalt(inhalt: dict[str, Any]) -> str:
    teile = [f"<h2>{html.escape(str(inhalt.get('titel') or 'Ohne Titel'))}</h2>"]
    kapitel = inhalt.get("kapitel")
    if isinstance(kapitel, list):
        teile.append("<ol>")
        for k in kapitel:
            lektionen = "".join(f"<li>{html.escape(str(t))}</li>" for t in k.get("lektionen", []))
            teile.append(
                f"<li><strong>{html.escape(str(k.get('titel')))}</strong> "
                f"({int(k.get('aufgaben') or 0)} Aufgaben)<ul>{lektionen}</ul></li>"
            )
        teile.append("</ol>")
    else:
        teile.append("<p>Kapitel und Lektionen sind unbekannt.</p>")
    hinweise = inhalt.get("ki_hinweise") or []
    if hinweise:
        teile.append(
            "<h3>Hinweise der KI-Pruefung (halten den Kurs nicht auf)</h3><ul>"
            + "".join(f"<li>{html.escape(str(h))}</li>" for h in hinweise)
            + "</ul>"
        )
    unbelegt = inhalt.get("unbelegte_aussagen") or []
    teile.append(
        f"<p>Unbelegte Aussagen: {len(unbelegt)}</p>"
        f"<p>Auftrag {html.escape(str(inhalt.get('job_id')))}, Revision "
        f"{int(inhalt.get('revision') or 0)}, Kursrevision {int(inhalt.get('kurs_revision') or 0)}</p>"
    )
    return "".join(teile)


async def _koerper(receive: Receive) -> dict[str, list[str]]:
    data = b""
    while True:
        message = await receive()
        data += message.get("body", b"")
        if not message.get("more_body") or len(data) > 4096:
            break
    return parse_qs(data.decode("utf-8", "replace"))


_KNOPF_STIL = "font-size:1.1rem;padding:.8rem 1.4rem;min-height:3rem"


async def _antwort(
    send: Send, status: int, titel: str, text: str, *, knopf: bool | None = None,
    knopf_text: str = "Jetzt veroeffentlichen",
) -> None:
    """``knopf``: True an active button, False a disabled one, None none."""
    if knopf:
        form = (
            f'<form method="post"><button type="submit" style="{_KNOPF_STIL}">'
            f"{html.escape(knopf_text)}</button></form>"
        )
    elif knopf is False:
        form = (
            f'<form><button type="button" disabled style="{_KNOPF_STIL}">'
            f"{html.escape(knopf_text)}</button></form>"
        )
    else:
        form = ""
    seite = (
        "<!doctype html><html lang=\"de\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>"
        f"{html.escape(titel)}</title><body style=\"font-family:sans-serif;max-width:40rem;"
        f"margin:2rem auto;padding:0 1rem\"><h1>{html.escape(titel)}</h1>{text}{form}</body></html>"
    ).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [*_HEADERS, (b"content-length", str(len(seite)).encode())]})
    await send({"type": "http.response.body", "body": seite})
