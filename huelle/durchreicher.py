"""Durchreicher: ASGI-Reverse-Proxy auf PenEcho (eigene Flaechen-Herkunft, einbettbar), mit KI-Sperre."""

from __future__ import annotations

import asyncio
import html
import inspect
import json
import logging
import posixpath
import re
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import websockets

from .sperre import CLOUD_PFADE, KI_PFADE, Sperre
from .zugang import COOKIE

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

log = logging.getLogger(__name__)

SPERR_TEXT = "Während der Aufgabe hilft „Erklär mir das“."
NICHT_ERREICHBAR = "Die Fläche ist gerade nicht erreichbar."
NICHT_ERLAUBT = "Nicht erlaubt."

_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})
# Vom Websocket-Handshake selbst erzeugt, nicht weiterreichen.
_WS_KOPF = frozenset({
    "host", "origin", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept",
})
_FRAME = re.compile(r"frame-ancestors\s+'none'", re.IGNORECASE)
# Direktive mit 'self' (z. B. widget-host.html in PenEcho): Browser pruefen JEDEN Vorfahren, die Huelle-Seite
# (andere Herkunft als die Flaeche) ist der oberste, also muss sie dort mit eingetragen sein.
_FRAME_SELF = re.compile(r"(frame-ancestors)([^;]*)", re.IGNORECASE)


_VERBOTEN = (chr(92).encode(), bytes([9]), bytes([13]), bytes([10]))  # Backslash, Tab, CR, LF

# Speichern ueber PenEchos eigenen Knopf: der Durchreicher liefert das Skript selbst aus (gleiche
# Herkunft wie PenEchos Seite, also von deren `script-src 'self'` gedeckt) und traegt es in PenEchos
# Hauptseite ein. Der Namensraum `/__huelle/` geht nie an PenEcho.
HUELLE_PRAEFIX = "/__huelle/"
SPEICHERN_PFAD = "/__huelle/speichern.js"
SPEICHERN_SKRIPT = Path(__file__).parent / "flaeche" / "speichern.js"
# Nur PenEchos Hauptseite (main.js: `/` -> index.html, sonst access.html); Widget-Rahmen bleiben unberuehrt.
_SEITEN_PFADE = frozenset({"/", "/index.html"})
_MAX_SEITE = 16 * 1024 * 1024
# PenEcho fragt nach jedem Neustart einmal, ob es mit Code oder offen laufen soll (nur im Speicher).
# Die Huelle waehlt offen: PenEcho lauscht nur auf 127.0.0.1, den Zugang regelt die Huelle selbst.
_ZUGANG_STATUS = "/api/local-access/status"
_ZUGANG_OFFEN = "/api/local-access/open"
_KOPF_ENDE = re.compile(rb"</head\s*>", re.IGNORECASE)
_BODY_ENDE = re.compile(rb"</body\s*>", re.IGNORECASE)
_SKRIPT_SRC = re.compile(r"(?<![\w-])(script-src(?:-elem)?)(\s[^;]*)?(?=;|$)", re.IGNORECASE)
_DEFAULT_SRC = re.compile(r"(?<![\w-])default-src(\s[^;]*)?(?=;|$)", re.IGNORECASE)


def skript_einfuegen(html: bytes, tag: bytes) -> bytes | None:
    """`tag` vor `</head>` (sonst vor dem letzten `</body>`); None, wenn das nicht geht oder schon drin ist."""
    if SPEICHERN_PFAD.encode() in html:
        return None
    treffer = _KOPF_ENDE.search(html)
    if treffer is None:
        alle = list(_BODY_ENDE.finditer(html))
        treffer = alle[-1] if alle else None
    if treffer is None:
        return None
    return html[:treffer.start()] + tag + html[treffer.start():]


def skript_erlauben(csp: str) -> str:
    """Sorgt dafuer, dass `script-src` (und `script-src-elem`) Skripte der eigenen Herkunft erlaubt.

    PenEcho 1.4.2 erlaubt sie schon (`script-src 'self' ...`); dann bleibt die Richtlinie gleich.
    """
    def ergaenzt(m: re.Match[str]) -> str:
        quellen = (m.group(2) or "").split()
        if "'self'" in (q.lower() for q in quellen):
            return m.group(0)
        quellen = [q for q in quellen if q.lower() != "'none'"] + ["'self'"]
        return m.group(1) + " " + " ".join(quellen)

    neu = _SKRIPT_SRC.sub(ergaenzt, csp)
    if not _SKRIPT_SRC.search(csp):
        m = _DEFAULT_SRC.search(csp)
        if m and "'self'" not in (m.group(1) or "").lower():
            neu = csp.rstrip().rstrip(";") + "; script-src 'self'"
    return neu


def pfad_unzulaessig(scope: Scope) -> bool:
    """Node (WHATWG-URL) macht aus Backslash ein `/` und streicht Tab/CR/LF; Browser senden das nie.

    Solche Pfade werden abgewiesen, statt sie zu kanonisieren, damit Pruefung und Weitergabe nie auseinanderlaufen.
    Ebenso Pfade, die nicht mit `/` beginnen (`*`, absolute Form): sie ergaeben mit der Basis keine Adresse.
    """
    roh = scope.get("raw_path") or scope["path"].encode("utf-8")
    return not roh.startswith(b"/") or any(z in roh for z in _VERBOTEN)


def _trifft(pfad: str, praefixe_roh: tuple[str, ...]) -> bool:
    """Prueft roh und normalisiert (Node loest `..` und %2e auf), ohne Gross-/Kleinschreibung."""
    pfad = pfad.replace("\\", "/")
    kandidaten = {pfad, unquote(pfad)}
    for k in list(kandidaten):
        kandidaten.add(posixpath.normpath(k))
        kandidaten.add(posixpath.normpath("/" + k.lstrip("/")))
    praefixe = tuple(p.casefold() for p in praefixe_roh)
    return any(k.casefold().startswith(praefixe) for k in kandidaten)


def ist_ki_pfad(pfad: str) -> bool:
    return _trifft(pfad, KI_PFADE)


def ist_cloud_pfad(pfad: str) -> bool:
    """Cloud-Anmeldung und -Kopplung: immer verboten, unabhaengig von der Sperre."""
    return _trifft(pfad, CLOUD_PFADE)


def _ohne_zugang(wert: str) -> str | None:
    """Der Cookie-Kopf ohne das Zugangscookie der Huelle; None, wenn nichts uebrig bleibt."""
    rest = [t.strip() for t in wert.split(";")]
    rest = [t for t in rest if t and t.split("=", 1)[0].strip() != COOKIE]
    return "; ".join(rest) or None


def _kopf_argument(connect: Callable[..., Any]) -> str:
    """websockets >= 14 nennt zusaetzliche Kopfzeilen `additional_headers`, aeltere `extra_headers`."""
    try:
        parameter = inspect.signature(connect).parameters
    except (TypeError, ValueError):
        return "extra_headers"
    return "additional_headers" if "additional_headers" in parameter else "extra_headers"


_WS_KOPF_ARGUMENT = _kopf_argument(websockets.connect)


async def _klartext(send: Send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]})
    await send({"type": "http.response.body", "body": body})


def _pfad_und_query(scope: Scope) -> str:
    pfad = (scope.get("raw_path") or scope["path"].encode("utf-8")).decode("latin-1")
    if scope.get("query_string"):
        pfad += "?" + scope["query_string"].decode("latin-1")
    return pfad


class Durchreicher:
    """`eigene_herkuenfte`: die Flaechen-Herkuenfte, von denen PenEcho-Seiten anfragen duerfen.

    `rahmen_herkuenfte`: die Seiten-Herkuenfte, die PenEcho einbetten duerfen (CSP `frame-ancestors`);
    leer heisst: PenEchos `frame-ancestors 'none'` bleibt stehen.
    """

    def __init__(
        self, penecho_url: str, sperre: Sperre, eigene_herkuenfte: frozenset[str],
        *, rahmen_herkuenfte: tuple[str, ...] = (),
    ) -> None:
        teile = urlsplit(penecho_url)
        self._basis = f"{teile.scheme}://{teile.netloc}"
        self._netloc = teile.netloc
        self._ws_basis = ("wss" if teile.scheme == "https" else "ws") + f"://{teile.netloc}"
        self._sperre = sperre
        self._eigene = frozenset(h.lower() for h in eigene_herkuenfte)
        self._seiten = tuple(rahmen_herkuenfte)
        self._rahmen = "frame-ancestors " + (" ".join(rahmen_herkuenfte) if rahmen_herkuenfte else "'none'")
        # Ohne einbettende Seite gibt es niemanden, der „Speichern“ anfragt: dann kein Skript.
        self._tag: bytes | None = None
        if self._seiten:
            erlaubt = html.escape(" ".join(self._seiten), quote=True)
            self._tag = (f'<meta name="huelle-herkuenfte" content="{erlaubt}">'
                         f'<script type="module" src="{SPEICHERN_PFAD}"></script>').encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._ws(scope, receive, send)
        elif scope["type"] == "lifespan":
            while True:
                nachricht = await receive()
                if nachricht["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif nachricht["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

    # --- HTTP ---------------------------------------------------------------------------

    def _origin_erlaubt(self, origin: str) -> bool:
        """Exakter Vergleich (scheme://host[:port], Host klein); `null` und Pfadanhaenge fallen durch."""
        return origin.lower() in self._eigene

    def _anfrage_kopf(self, scope: Scope, *, unkomprimiert: bool = False) -> list[tuple[str, str]]:
        kopf: list[tuple[str, str]] = []
        hat_origin = False
        for roh_name, roh_wert in scope["headers"]:
            name, wert = roh_name.decode("latin-1").lower(), roh_wert.decode("latin-1")
            if name == "origin":
                hat_origin = True
            if name in _HOP or name in ("host", "origin"):
                continue
            if unkomprimiert and name == "accept-encoding":
                continue
            if name == "cookie":
                rest = _ohne_zugang(wert)
                if rest is None:
                    continue
                wert = rest
            kopf.append((name, wert))
        kopf.append(("host", self._netloc))
        if hat_origin:
            kopf.append(("origin", self._basis))
        if unkomprimiert:
            kopf.append(("accept-encoding", "identity"))
        return kopf

    def _mit_seiten(self, m: re.Match[str]) -> str:
        quellen = m.group(2).split()
        if "'self'" not in (q.lower() for q in quellen):
            return m.group(0)
        neu = [h for h in self._seiten if h not in quellen]
        return m.group(0) if not neu else m.group(1) + " " + " ".join(quellen + neu)

    def _antwort_kopf(self, roh: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        aus: list[tuple[bytes, bytes]] = []
        for name, wert in roh:
            if name.decode("latin-1").lower() in _HOP:
                continue
            if name.lower() == b"content-security-policy":
                text = _FRAME.sub(lambda _m: self._rahmen, wert.decode("latin-1"))
                wert = _FRAME_SELF.sub(self._mit_seiten, text).encode("latin-1")
            aus.append((name, wert))
        return aus

    async def _eigene_datei(self, scope: Scope, send: Send) -> None:
        """`/__huelle/speichern.js` aus `huelle/flaeche/`; alles andere unter `/__huelle` ist 404."""
        roh = (scope.get("raw_path") or scope["path"].encode("utf-8"))
        if roh != SPEICHERN_PFAD.encode():
            await _klartext(send, 404, "Nicht gefunden")
            return
        if scope["method"] not in ("GET", "HEAD"):
            await _klartext(send, 405, "Nicht erlaubt.")
            return
        body = SPEICHERN_SKRIPT.read_bytes()
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/javascript; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]})
        await send({"type": "http.response.body", "body": body if scope["method"] == "GET" else b""})

    async def _seite_mit_skript(
        self, antwort: httpx.Response, roh: AsyncIterator[bytes], kopf: list[tuple[bytes, bytes]],
    ) -> tuple[tuple[list[tuple[bytes, bytes]], bytes] | None, list[bytes]]:
        """Liest PenEchos Seite und traegt das Skript ein.

        Ergebnis: ((neuer Kopf, neuer Body) oder None, schon gelesene Rohstuecke). Bei None geht die Seite
        unveraendert weiter: zu gross, unbekannte Kompression, kaputtes gzip oder keine Einfuegestelle.
        """
        stuecke: list[bytes] = []
        groesse = 0
        async for stueck in roh:
            stuecke.append(stueck)
            groesse += len(stueck)
            if groesse > _MAX_SEITE:
                log.warning("PenEcho-Seite zu gross, Speichern-Skript nicht eingetragen")
                return None, stuecke
        kodierung = antwort.headers.get("content-encoding", "identity").strip().lower()
        daten = b"".join(stuecke)
        if kodierung == "gzip":
            entpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                daten = entpacker.decompress(daten, _MAX_SEITE)
            except zlib.error:
                log.warning("PenEcho-Seite: gzip unlesbar, Speichern-Skript nicht eingetragen")
                return None, stuecke
            if entpacker.unconsumed_tail or not entpacker.eof:
                log.warning("PenEcho-Seite: gzip zu gross oder unvollstaendig, Skript nicht eingetragen")
                return None, stuecke
        elif kodierung not in ("", "identity"):
            log.warning("PenEcho-Seite mit Kompression %s, Speichern-Skript nicht eingetragen", kodierung[:20])
            return None, stuecke
        neu = skript_einfuegen(daten, self._tag) if self._tag is not None else None
        if neu is None:
            log.warning("PenEcho-Seite ohne </head> und </body>, Speichern-Skript nicht eingetragen")
            return None, stuecke
        aus: list[tuple[bytes, bytes]] = []
        for name, wert in kopf:
            klein = name.lower()
            if klein in (b"content-length", b"content-encoding", b"etag"):
                continue
            if klein == b"content-security-policy":
                wert = skript_erlauben(wert.decode("latin-1")).encode("latin-1")
            aus.append((name, wert))
        aus.append((b"content-length", str(len(neu)).encode("ascii")))
        return (aus, neu), stuecke

    async def _zugang_oeffnen(self) -> None:
        """Steht PenEchos Zugang auf „unentschieden“, waehlt die Huelle „offen“; Fehler nur protokollieren."""
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                stand = await client.get(self._basis + _ZUGANG_STATUS)
                if stand.status_code != 200 or stand.json().get("mode") != "undecided":
                    return
                antwort = await client.post(self._basis + _ZUGANG_OFFEN, json={"acknowledgeRisk": True},
                                            headers={"origin": self._basis})
                if antwort.status_code != 200:
                    log.warning("PenEcho-Zugang nicht geoeffnet: HTTP %s", antwort.status_code)
        except (httpx.HTTPError, ValueError, AttributeError) as fehler:
            log.warning("PenEcho-Zugang nicht geprueft: %s", type(fehler).__name__)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        for roh_name, roh_wert in scope["headers"]:
            if roh_name.lower() == b"origin" and not self._origin_erlaubt(roh_wert.decode("latin-1")):
                await _klartext(send, 403, NICHT_ERLAUBT)
                return
        if pfad_unzulaessig(scope):
            await _klartext(send, 400, "Ungültiger Pfad")
            return
        if ist_cloud_pfad(scope["path"]):
            await _klartext(send, 403, NICHT_ERLAUBT)
            return
        if self._sperre.gesperrt() and ist_ki_pfad(scope["path"]):
            body = json.dumps({"error": SPERR_TEXT}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 423, "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ]})
            await send({"type": "http.response.body", "body": body})
            return
        if _trifft(scope["path"], ("/__huelle",)):
            await self._eigene_datei(scope, send)
            return
        einfuegen = (self._tag is not None and scope["method"] in ("GET", "HEAD")
                     and scope["path"] in _SEITEN_PFADE)
        if scope["method"] in ("GET", "HEAD") and scope["path"] in _SEITEN_PFADE:
            await self._zugang_oeffnen()

        async def koerper() -> AsyncIterator[bytes]:
            while True:
                nachricht = await receive()
                if nachricht["type"] == "http.disconnect":
                    return
                if nachricht.get("body"):
                    yield nachricht["body"]
                if not nachricht.get("more_body"):
                    return

        url = self._basis + _pfad_und_query(scope)
        gestartet = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0), follow_redirects=False) as client:
                # Nur weiterreichen, was der Browser angeboten hat (httpx setzte sonst eigenes gzip).
                client.headers.pop("accept-encoding", None)
                anfrage = client.build_request(
                    scope["method"], url, headers=self._anfrage_kopf(scope, unkomprimiert=einfuegen),
                    content=koerper() if scope["method"] not in ("GET", "HEAD") else None,
                )
                antwort = await client.send(anfrage, stream=True)
                try:
                    kopf = self._antwort_kopf(antwort.headers.raw)
                    roh = antwort.aiter_raw()
                    vorab: list[bytes] = []
                    art = antwort.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if einfuegen and antwort.status_code == 200 and art == "text/html":
                        if scope["method"] == "HEAD":  # Laenge der veraenderten Seite ist hier unbekannt
                            kopf = [(n, w) for n, w in kopf if n.lower() not in (b"content-length", b"etag")]
                        else:
                            fertig, vorab = await self._seite_mit_skript(antwort, roh, kopf)
                            if fertig is not None:
                                neu_kopf, body = fertig
                                await send({"type": "http.response.start", "status": 200, "headers": neu_kopf})
                                gestartet = True
                                await send({"type": "http.response.body", "body": body})
                                return
                    await send({"type": "http.response.start", "status": antwort.status_code, "headers": kopf})
                    gestartet = True
                    for stueck in vorab:
                        await send({"type": "http.response.body", "body": stueck, "more_body": True})
                    async for stueck in roh:
                        await send({"type": "http.response.body", "body": stueck, "more_body": True})
                    await send({"type": "http.response.body", "body": b""})
                finally:
                    await antwort.aclose()
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError):
            if gestartet:
                return  # Antwort lief schon; der Abbruch zeigt sich dem Browser als Abbruch
            await _klartext(send, 502, NICHT_ERREICHBAR)

    # --- Websocket ----------------------------------------------------------------------

    async def _ws(self, scope: Scope, receive: Receive, send: Send) -> None:
        nachricht = await receive()
        if nachricht["type"] != "websocket.connect":
            return
        origin = next((w.decode("latin-1") for n, w in scope["headers"] if n.lower() == b"origin"), None)
        if origin is None or not self._origin_erlaubt(origin):
            await send({"type": "websocket.close", "code": 1008})
            return
        if pfad_unzulaessig(scope) or ist_cloud_pfad(scope["path"]):
            await send({"type": "websocket.close", "code": 1008})
            return
        ki_pfad = ist_ki_pfad(scope["path"])
        if ki_pfad and self._sperre.gesperrt():
            await send({"type": "websocket.close", "code": 1008, "reason": SPERR_TEXT})
            return
        kopf: list[tuple[str, str]] = []
        for n, w in scope["headers"]:
            name, wert = n.decode("latin-1").lower(), w.decode("latin-1")
            if name in _WS_KOPF | _HOP:
                continue
            if name == "cookie":
                rest = _ohne_zugang(wert)
                if rest is None:
                    continue
                wert = rest
            kopf.append((name, wert))
        try:
            upstream = await websockets.connect(
                self._ws_basis + _pfad_und_query(scope), origin=self._basis, **{_WS_KOPF_ARGUMENT: kopf},
                subprotocols=list(scope.get("subprotocols") or []) or None,
                open_timeout=10, max_size=None,
            )
        except Exception:  # Verbindungs-, Handshake- oder Timeout-Fehler: einheitlich "weg"
            await send({"type": "websocket.close", "code": 1011})
            return
        await send({"type": "websocket.accept", "subprotocol": upstream.subprotocol})

        closed = asyncio.Event()

        async def hoch() -> None:
            while True:
                n = await receive()
                if n["type"] == "websocket.disconnect":
                    return
                if n["type"] == "websocket.receive":
                    if ki_pfad and self._sperre.gesperrt():  # Sperre kann nach dem Verbindungsaufbau greifen
                        await send({"type": "websocket.close", "code": 1008, "reason": SPERR_TEXT})
                        closed.set()
                        return
                    if n.get("text") is not None:
                        await upstream.send(n["text"])
                    elif n.get("bytes") is not None:
                        await upstream.send(n["bytes"])

        async def runter() -> None:
            async for m in upstream:
                if isinstance(m, str):
                    await send({"type": "websocket.send", "text": m})
                else:
                    await send({"type": "websocket.send", "bytes": bytes(m)})

        aufgaben = [asyncio.ensure_future(hoch()), asyncio.ensure_future(runter())]
        try:
            await asyncio.wait(aufgaben, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for a in aufgaben:
                a.cancel()
            await asyncio.gather(*aufgaben, return_exceptions=True)
            await upstream.close()
            try:
                if not closed.is_set():
                    await send({"type": "websocket.close", "code": 1000})
            except Exception:  # Gegenseite schon zu
                pass
