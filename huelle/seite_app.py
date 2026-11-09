"""Die Seite der Lern-Huelle: liefert `huelle/seite/` unter `/lernen/` mit CSP und Sicherheits-Headern.

Die Flaeche (PenEcho) kommt von der eigenen Flaechen-Herkunft (Ruling J); ihre Adresse steht nicht in den
Dateien, sondern wird beim Ausliefern in `index.html` eingesetzt.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from .einstellung import Einstellung

SEITE = Path(__file__).parent / "seite"
PREFIX = "/lernen/"
KOPF_MARKE = "<!--KOPF-->"
FLAECHE_MARKE = "<!--FLAECHE-->"
# KaTeX (MIT, 0.16.11, dieselbe Fassung wie die Claw-Seiten) liegt bei der Seite: kein fremder Host beim Laden.
# Die Huelle setzt Formeln selbst (aufgabe.js `formel` ueber window.katex); Auto-Render braucht sie nicht.
KOPF = '<link rel="stylesheet" href="katex/katex.min.css"><script defer src="katex/katex.min.js"></script>'

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

# Pfad -> (Datei, Content-Type). Nur diese Dateien werden ausgeliefert.
DATEIEN = {
    "": ("index.html", "text/html; charset=utf-8"),
    "huelle.css": ("huelle.css", "text/css; charset=utf-8"),
    "huelle.js": ("huelle.js", "text/javascript; charset=utf-8"),
    "aufgabe.js": ("aufgabe.js", "text/javascript; charset=utf-8"),
    "flaeche.js": ("flaeche.js", "text/javascript; charset=utf-8"),
    "liste.js": ("liste.js", "text/javascript; charset=utf-8"),
    "katex/katex.min.css": ("katex/katex.min.css", "text/css; charset=utf-8"),
    "katex/katex.min.js": ("katex/katex.min.js", "text/javascript; charset=utf-8"),
    **{f"katex/fonts/{f.name}": (f"katex/fonts/{f.name}", "font/woff2")
       for f in sorted((SEITE / "katex" / "fonts").glob("*.woff2"))},
}


def seiten_csp(flaechen_herkuenfte: Iterable[str]) -> bytes:
    """Nur eigene Skripte, Styles (KaTeX setzt Inline-Styles) und Schriften, dazu Fetch, Bilder und die Flaeche."""
    return "; ".join([
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "font-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "connect-src 'self'",
        "img-src 'self'",
        "frame-src " + " ".join(flaechen_herkuenfte),
    ]).encode()


def _headers(content_type: str, laenge: int, richtlinie: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(laenge).encode("ascii")),
        (b"content-security-policy", richtlinie),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"cache-control", b"no-store"),
    ]


def seite_app(einstellung: Einstellung) -> App:
    richtlinie = seiten_csp(einstellung.flaechen_herkuenfte)
    lokal_host = f"127.0.0.1:{einstellung.port}"
    flaeche_basis, flaeche_lokal = einstellung.flaechen_herkuenfte

    def seite_html(host: str) -> str:
        # Wer die Seite lokal oeffnet, bekommt die Flaeche lokal; sonst die Flaechen-Herkunft.
        flaeche = flaeche_lokal if host == lokal_host else flaeche_basis
        roh = (SEITE / "index.html").read_text(encoding="utf-8")
        return roh.replace(KOPF_MARKE, KOPF).replace(FLAECHE_MARKE, html.escape(flaeche, quote=True))

    async def text(send: Send, status: int, inhalt: str, erlaubt: str | None = None) -> None:
        body = inhalt.encode("utf-8")
        headers = _headers("text/plain; charset=utf-8", len(body), richtlinie)
        if erlaubt:
            headers.append((b"allow", erlaubt.encode("ascii")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        if scope.get("method") not in ("GET", "HEAD"):
            await text(send, 405, "Nur Anzeigen.", "GET, HEAD")
            return
        path = scope.get("path", "")
        if path == "/lernen":
            path = PREFIX
        name = path[len(PREFIX):] if path.startswith(PREFIX) else None
        if name not in DATEIEN:
            await text(send, 404, "Nicht gefunden")
            return
        datei, art = DATEIEN[name]
        if name == "":
            host = next((w.decode("latin-1").lower() for n, w in scope.get("headers", []) if n.lower() == b"host"), "")
            body = seite_html(host).encode("utf-8")
        else:
            body = (SEITE / datei).read_bytes()
        kopf = b"" if scope["method"] == "HEAD" else body
        await send({"type": "http.response.start", "status": 200, "headers": _headers(art, len(body), richtlinie)})
        await send({"type": "http.response.body", "body": kopf})

    return app
