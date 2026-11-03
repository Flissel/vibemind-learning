"""Die Seite der Lern-Huelle: liefert `huelle/seite/` unter `/lernen/` mit CSP und Sicherheits-Headern."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..claw.formeln import KOPF, csp

SEITE = Path(__file__).parent / "seite"
PREFIX = "/lernen/"
KOPF_MARKE = "<!--KOPF-->"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

# Pfad -> (Datei, Content-Type). Nur diese Dateien werden ausgeliefert.
DATEIEN = {
    "": ("index.html", "text/html; charset=utf-8"),
    "huelle.css": ("huelle.css", "text/css; charset=utf-8"),
    "huelle.js": ("huelle.js", "text/javascript; charset=utf-8"),
    "aufgabe.js": ("aufgabe.js", "text/javascript; charset=utf-8"),
}


def seiten_csp() -> bytes:
    """Die CSP der Claw-Seiten (KaTeX), erweitert um eigene Skripte/Styles, Fetch und den Flaechen-Rahmen."""
    direktiven = [d for d in csp().decode().split("; ") if d]
    ergebnis: list[str] = []
    for d in direktiven:
        if d.startswith("script-src ") or d.startswith("style-src "):
            d = d.replace(" ", " 'self' ", 1)
        ergebnis.append(d)
    ergebnis += ["connect-src 'self'", "frame-src 'self'"]
    return "; ".join(ergebnis).encode()


def _headers(content_type: str, laenge: int) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(laenge).encode("ascii")),
        (b"content-security-policy", seiten_csp()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"cache-control", b"no-store"),
    ]


def _seite_html() -> str:
    return (SEITE / "index.html").read_text(encoding="utf-8").replace(KOPF_MARKE, KOPF)


async def _text(send: Send, status: int, text: str, erlaubt: str | None = None) -> None:
    body = text.encode("utf-8")
    headers = _headers("text/plain; charset=utf-8", len(body))
    if erlaubt:
        headers.append((b"allow", erlaubt.encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def seite_app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    if scope["type"] != "http":
        return
    if scope.get("method") not in ("GET", "HEAD"):
        await _text(send, 405, "Nur Anzeigen.", "GET, HEAD")
        return
    path = scope.get("path", "")
    if path == "/lernen":
        path = PREFIX
    name = path[len(PREFIX):] if path.startswith(PREFIX) else None
    if name not in DATEIEN:
        await _text(send, 404, "Nicht gefunden")
        return
    datei, art = DATEIEN[name]
    if name == "":
        body = _seite_html().encode("utf-8")
    else:
        body = (SEITE / datei).read_bytes()
    kopf = b"" if scope["method"] == "HEAD" else body
    await send({"type": "http.response.start", "status": 200, "headers": _headers(art, len(body))})
    await send({"type": "http.response.body", "body": kopf})
