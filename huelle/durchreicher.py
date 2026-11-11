"""Durchreicher: ASGI-Reverse-Proxy auf PenEcho (eigene Flaechen-Herkunft, einbettbar), mit KI-Sperre."""

from __future__ import annotations

import asyncio
import html
import inspect
import json
import logging
import posixpath
import re
import time
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx
import websockets

from .gruppe import (
    AUSWEIS_GUELTIG_S,
    GRUPPE_UNGUELTIG,
    GRUPPE_WEG,
    KEIN_MITGLIED,
    ZUGANG_WEG,
    Gruppen,
    KeinMitglied,
    Mitgliedschaft,
    ausweis,
    flaeche_der_gruppe,
    gruppe_aus_flaeche,
    person_kennung,
)
from .katalog import UebersichtWeg
from .person import PERSON_COOKIE
from .sperre import CLOUD_PFADE, KI_PFADE, Sperre
from .zugang import COOKIE

# Die eigenen Cookies der Huelle (Notweg und Person) gehen nie an PenEcho.
_HUELLE_COOKIES = frozenset({COOKIE, PERSON_COOKIE})

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
# PenEchos Server-Ablage (lernflaeche src/server/main.js ca. 3993-4045): Liste, Board je id, Vorschau, Projekte.
# Fuer Personen (nicht den Inhaber) nur die eigenen Boards, siehe `Durchreicher._ablage_person`.
ABLAGE_PFADE = ("/api/canvases", "/api/canvas-projects")
_ABLAGE_ID = re.compile(r"/api/canvases/(\d{10,16}-[a-zA-Z0-9-]{8,64})(/preview)?")
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


# Lernmodus-Allowlist fuer Personen (nicht den Inhaber). Ermittelt aus der Lernflaeche (src/client/app,
# public/index.html, src/server/main.js); Fundstellen im Bericht zur Abschluss-Korrektur, Runde 2.
# /api/canvases und /api/canvas-projects gehen weiter an die Ablage-Pruefung (`_ablage_person`).
_LERN_API: dict[str, frozenset[str]] = {
    "/api/config.js": frozenset({"GET", "HEAD"}),   # <script> in index.html
    "/api/config": frozenset({"GET"}),
    "/api/ai/command": frozenset({"POST"}),         # KI beim Ueben (ai-runtime.js); die KI-Sperre steht davor
    "/api/suggest": frozenset({"POST"}),            # Vorschlaege (smart-suggestions.js); KI-Sperre davor
    "/api/suggest/status": frozenset({"GET"}),
    "/api/plugins": frozenset({"GET"}),             # Plugin-Katalog der Widgets (core.js loadPluginDocuments)
}
_LERN_ABLAGE = ("/api/canvases", "/api/canvas-projects")
_LERN_WS = frozenset({"/api/mcp/canvas", "/api/abgleich"})  # MCP-Flaeche (mcp-runtime.js), Abgleich (gruppe.js)
# Abgleich der Gruppenflaeche (Teil C): fuer jede Kennung, auch den Inhaber, nur exakt dieser Rohpfad mit
# `flaeche=g-<id>` und `fenster`; die Huelle setzt Person und Ausweis selbst.
ABGLEICH_PFAD = "/api/abgleich"
KI_BEFEHL = "/api/ai/command"
# Nachpruefung offener Abgleich-Verbindungen: Takt plus Frist je Pruefung <= 60 s (Review-Fokus 3).
NACHPRUEFEN_S = 45
PRUEF_FRIST_S = 15
_FENSTER = re.compile(r"[a-z0-9-]{1,40}")  # KENNUNG des Abgleichdienstes (protokoll.mjs)
# Statisch erlaubt ist jedes GET/HEAD ausserhalb von /api/, ausser den privaten Plugins des Inhabers.
_STATISCH_GESPERRT = ("/plugins/private/",)


def _schlicht(roh: str) -> bool:
    """Ein Rohpfad, den PenEcho (WHATWG-URL) genau so sieht: ohne %-Kodierung, `//`, `.`- oder `..`-Segmente."""
    if not roh.startswith("/") or "%" in roh or "//" in roh:
        return False
    return not any(teil in (".", "..") for teil in roh.split("/")[1:])


def _ist_api(roh: str) -> bool:
    klein = roh.casefold()
    return klein == "/api" or klein.startswith("/api/")


def _rohpfad(scope: Scope) -> str:
    """Der rohe Pfad ohne Query, wie ihn der Durchreicher weiterreicht."""
    return (scope.get("raw_path") or scope["path"].encode("utf-8")).split(b"?", 1)[0].decode("latin-1")


def person_http_erlaubt(scope: Scope) -> bool:
    """Lernmodus-Allowlist fuer eine HTTP-Anfrage einer Person: exakte Rohpfade je Methode, sonst nichts unter /api/."""
    roh = _rohpfad(scope)
    methode = scope.get("method", "")
    if not _schlicht(roh):
        return False
    if _ist_api(roh):
        if any(roh == p or roh.startswith(p + "/") for p in _LERN_ABLAGE):
            return True  # die Ablage-Pruefung entscheidet je Methode und Board
        return methode in _LERN_API.get(roh, frozenset())
    if roh.casefold().startswith(_STATISCH_GESPERRT):
        return False
    return methode in ("GET", "HEAD")


def person_ws_erlaubt(scope: Scope) -> bool:
    """Websockets einer Person: nur MCP-Flaeche und Abgleich (exakter Rohpfad; Query erlaubt)."""
    roh = _rohpfad(scope)
    return _schlicht(roh) and roh in _LERN_WS


def ist_abgleich_pfad(scope: Scope) -> bool:
    """Zielt der Websocket (roh oder normalisiert, ohne Gross-/Kleinschreibung) auf den Abgleich?"""
    return _trifft(scope["path"], (ABGLEICH_PFAD,)) or _trifft(_rohpfad(scope), (ABGLEICH_PFAD,))


def abgleich_anfrage(query_string: bytes) -> tuple[int, str] | None:
    """(gruppe_id, fenster) aus der Query des Browsers: genau ein `flaeche=g-<id>` und ein `fenster`, sonst None.

    Weitere Parameter (person, ausweis, ...) werden ignoriert; die Huelle baut die Query neu.
    """
    try:
        werte = parse_qs(query_string.decode("ascii"), keep_blank_values=True, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    flaeche, fenster = werte.get("flaeche", []), werte.get("fenster", [])
    if len(flaeche) != 1 or len(fenster) != 1 or not _FENSTER.fullmatch(fenster[0]):
        return None
    gruppe_id = gruppe_aus_flaeche(flaeche[0])
    return None if gruppe_id is None else (gruppe_id, fenster[0])


def _ohne_zugang(wert: str) -> str | None:
    """Der Cookie-Kopf ohne die Cookies der Huelle (Zugang, Person); None, wenn nichts uebrig bleibt."""
    rest = [t.strip() for t in wert.split(";")]
    rest = [t for t in rest if t and t.split("=", 1)[0].strip() not in _HUELLE_COOKIES]
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


async def _json_antwort(send: Send, status: int, daten: dict[str, Any]) -> None:
    body = json.dumps(daten, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]})
    await send({"type": "http.response.body", "body": body})


async def _ablehnen(send: Send, code: int, grund: str) -> None:
    """Annehmen, dann sofort mit Code und Grund schliessen (ohne Upstream): so kommt der Code beim Browser an."""
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.close", "code": code, "reason": grund})


class _AblageWeg(Exception):
    """PenEchos Ablage antwortet nicht brauchbar (Netz, Status, kein JSON)."""


def _pfad_und_query(scope: Scope) -> str:
    pfad = (scope.get("raw_path") or scope["path"].encode("utf-8")).decode("latin-1")
    if scope.get("query_string"):
        pfad += "?" + scope["query_string"].decode("latin-1")
    return pfad


class Durchreicher:
    """`eigene_herkuenfte`: die Flaechen-Herkuenfte, von denen PenEcho-Seiten anfragen duerfen.

    `rahmen_herkuenfte`: die Seiten-Herkuenfte, die PenEcho einbetten duerfen (CSP `frame-ancestors`);
    leer heisst: PenEchos `frame-ancestors 'none'` bleibt stehen.

    `ablage`: Kennung -> documentIds, die diese Person in PenEchos Server-Ablage erreicht (None = alle, der
    Inhaber); die Kennung setzt die Flaechen-Weiche (`scope["state"]["actor_id"]`). Ohne `ablage` bleibt die
    Ablage ungefiltert.

    `inhaber`: die Inhaber-Kennung (Notweg). Ist sie gesetzt, gilt fuer jede andere Kennung (und ohne Kennung)
    die Lernmodus-Allowlist (`person_http_erlaubt`, `person_ws_erlaubt`); ohne `inhaber` keine Allowlist.

    `gruppen` und `abgleich_schluessel` (Teil C): der Websocket `/api/abgleich` geht nur mit beiden durch und nur
    fuer Mitglieder der Gruppe (`_abgleich_ziel`); ohne sie ist er fuer jede Kennung zu (1008). Offene Verbindungen
    werden alle `nachpruefen_s` Sekunden frisch nachgeprueft. `uhr` (Unix-Sekunden, Ablauf des Ausweises) und
    `schlafen` sind fuer Tests austauschbar.
    """

    def __init__(
        self, penecho_url: str, sperre: Sperre, eigene_herkuenfte: frozenset[str],
        *, rahmen_herkuenfte: tuple[str, ...] = (),
        ablage: Callable[[str], frozenset[str] | None] | None = None,
        inhaber: str | None = None,
        gruppen: Gruppen | None = None,
        abgleich_schluessel: str = "",
        uhr: Callable[[], float] = time.time,
        schlafen: Callable[[float], Awaitable[None]] = asyncio.sleep,
        nachpruefen_s: float = NACHPRUEFEN_S,
        monoton: Callable[[], float] = time.monotonic,
        pruef_frist_s: float = PRUEF_FRIST_S,
        ki_gleichzeitig: int = 3,
    ) -> None:
        # Freie KI (`/api/ai/command`) der eingeschraenkten Kennungen: je Kennung hoechstens eine Anfrage, gesamt
        # hoechstens `ki_gleichzeitig`; wer darueber liegt, wartet. Reihenfolge: erst die Sperre der Kennung,
        # dann der gemeinsame Platz; Freigabe umgekehrt.
        self._ki_gleichzeitig = max(1, ki_gleichzeitig)
        self._ki_platz: asyncio.Semaphore | None = None
        self._ki_je_kennung: dict[str, list[Any]] = {}
        self._monoton = monoton
        self._pruef_frist_s = pruef_frist_s
        self._ablage = ablage
        self._inhaber = inhaber
        self._gruppen = gruppen
        self._abgleich_schluessel = abgleich_schluessel.strip()
        self._uhr = uhr
        self._schlafen = schlafen
        self._nachpruefen_s = nachpruefen_s
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
        if self._eingeschraenkt(scope) and not person_http_erlaubt(scope):
            await _klartext(send, 403, NICHT_ERLAUBT)
            return
        if _trifft(scope["path"], ("/__huelle",)):
            await self._eigene_datei(scope, send)
            return
        if self._ablage is not None and _trifft(scope["path"], ABLAGE_PFADE):
            eigene = await self._eigene_boards(scope)
            if eigene is not None:
                await self._ablage_person(scope, receive, send, eigene)
                return
        if self._eingeschraenkt(scope) and _trifft(scope["path"], (KI_BEFEHL,)):
            await self._ki_begrenzt(scope, receive, send)
            return
        await self._weiterreichen(scope, receive, send)

    async def _ki_begrenzt(self, scope: Scope, receive: Receive, send: Send) -> None:
        """`/api/ai/command` einer Person: unter der Sperre der Kennung und dem gemeinsamen Platz.

        Wartende werden nie abgewiesen. Trennt der Browser, waehrend er wartet oder die Antwort noch laeuft,
        wird die Anfrage abgebrochen; die Sperren werden immer in umgekehrter Reihenfolge freigegeben.
        """
        state = scope.get("state")
        actor = state.get("actor_id") if isinstance(state, dict) else None
        kennung = actor if isinstance(actor, str) else ""
        eingang: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        getrennt = asyncio.Event()

        async def pumpe() -> None:
            while True:
                nachricht = await receive()
                eingang.put_nowait(nachricht)
                if nachricht["type"] == "http.disconnect":
                    getrennt.set()
                    return

        async def lesen() -> dict[str, Any]:
            return await eingang.get()

        if self._ki_platz is None:
            self._ki_platz = asyncio.Semaphore(self._ki_gleichzeitig)
        platz = self._ki_platz

        async def arbeit() -> None:
            eintrag = self._ki_je_kennung.get(kennung)
            if eintrag is None:
                eintrag = self._ki_je_kennung[kennung] = [asyncio.Lock(), 0]
            eintrag[1] += 1
            try:
                async with eintrag[0]:
                    async with platz:
                        await self._weiterreichen(scope, lesen, send)
            finally:
                eintrag[1] -= 1
                if eintrag[1] == 0 and self._ki_je_kennung.get(kennung) is eintrag:
                    del self._ki_je_kennung[kennung]

        lauscher = asyncio.ensure_future(pumpe())
        haupt = asyncio.ensure_future(arbeit())
        abbruch = asyncio.ensure_future(getrennt.wait())
        try:
            await asyncio.wait({haupt, abbruch}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for aufgabe in (haupt, abbruch, lauscher):
                if not aufgabe.done():
                    aufgabe.cancel()
            await asyncio.gather(haupt, abbruch, lauscher, return_exceptions=True)
        if haupt.done() and not haupt.cancelled() and haupt.exception() is not None:
            raise haupt.exception()  # type: ignore[misc]

    async def _weiterreichen(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Die (gepruefte) Anfrage an PenEcho und die Antwort zurueck, gestreamt."""
        einfuegen =(self._tag is not None and scope["method"] in ("GET", "HEAD")
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

    def _eingeschraenkt(self, scope: Scope) -> bool:
        """Gilt fuer diese Anfrage die Lernmodus-Allowlist? Ja fuer jede Kennung ausser dem Inhaber."""
        if self._inhaber is None:
            return False
        state = scope.get("state")
        return (state.get("actor_id") if isinstance(state, dict) else None) != self._inhaber

    # --- Server-Ablage (Boards) je Person ----------------------------------------------

    async def _eigene_boards(self, scope: Scope) -> frozenset[str] | None:
        """documentIds der Person dieser Anfrage; None = nicht eingeschraenkt (Inhaber). Ohne Kennung: nichts."""
        state = scope.get("state")
        actor = state.get("actor_id") if isinstance(state, dict) else None
        if not isinstance(actor, str) or not actor or self._ablage is None:
            return frozenset()
        return await asyncio.to_thread(self._ablage, actor)

    async def _ablage_person(self, scope: Scope, receive: Receive, send: Send, eigene: frozenset[str]) -> None:
        """PenEchos Server-Ablage fuer eine Person: nur ihre Boards; was der Lernmodus nicht braucht, ist 403.

        Lernmodus (lernflaeche persistence.js / canvas-documents.js): Speichern legt mit POST an und ueberschreibt
        mit PUT /<id>; ein verlorenes Board findet der Browser ueber die Liste (`metadataOnly=1`, documentId) und
        laedt es mit GET /<id>; Umbenennen ist PATCH /<id>. Gepruefte Pfade sind exakt (roh), sonst 403.
        """
        roh = _rohpfad(scope)
        methode = scope["method"]
        if roh == "/api/canvases":
            if methode == "POST":  # neues Board; PenEcho lehnt eine vorhandene id ab (409), ueberschreibt nie
                await self._weiterreichen(scope, receive, send)
                return
            if methode == "GET":  # jede Query wird zu metadataOnly=1, die Antwort nur {"canvases": [...]}
                await self._liste_gefiltert(scope, send, eigene)
                return
        else:
            treffer = _ABLAGE_ID.fullmatch(roh)
            if treffer is not None and methode in (("GET",) if treffer.group(2) else ("GET", "PUT", "PATCH")):
                try:
                    dokument = await self._dokument_von(scope, treffer.group(1))
                except _AblageWeg:
                    await _json_antwort(send, 502, {"error": NICHT_ERREICHBAR})
                    return
                if dokument is not None and dokument in eigene:
                    await self._weiterreichen(scope, receive, send)
                    return
        await _json_antwort(send, 403, {"error": NICHT_ERLAUBT})

    def _ablage_kopf(self, scope: Scope) -> dict[str, str]:
        """Kopf fuer eigene Lese-Anfragen an die Ablage: Host wie beim Durchreichen, Cookies ohne die der Huelle."""
        kopf = {"host": self._netloc, "accept": "application/json", "accept-encoding": "identity"}
        for name, wert in scope["headers"]:
            if name.lower() == b"cookie":
                rest = _ohne_zugang(wert.decode("latin-1"))
                if rest is not None:
                    kopf["cookie"] = rest
        return kopf

    async def _ablage_lesen(self, scope: Scope, pfad_und_query: str) -> tuple[int, Any]:
        """(Status, JSON) einer Lese-Anfrage an PenEchos Ablage; _AblageWeg bei Netzfehler oder kaputter Antwort."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False) as client:
                antwort = await client.get(self._basis + pfad_und_query, headers=self._ablage_kopf(scope))
            return antwort.status_code, antwort.json()
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError, ValueError) as fehler:
            log.warning("PenEcho-Ablage nicht lesbar: %s", type(fehler).__name__)
            raise _AblageWeg() from None

    async def _dokument_von(self, scope: Scope, canvas_id: str) -> str | None:
        """documentId des gespeicherten Boards `canvas_id` laut PenEchos Liste; None, wenn es keins (mehr) gibt."""
        status, daten = await self._ablage_lesen(scope, "/api/canvases?metadataOnly=1")
        boards = daten.get("canvases") if isinstance(daten, dict) else None
        if status != 200 or not isinstance(boards, list):
            log.warning("PenEcho-Ablage ohne Liste: HTTP %s", status)
            raise _AblageWeg()
        for board in boards:
            if isinstance(board, dict) and board.get("id") == canvas_id:
                dokument = board.get("documentId")
                return dokument if isinstance(dokument, str) and dokument else None
        return None

    async def _liste_gefiltert(self, scope: Scope, send: Send, eigene: frozenset[str]) -> None:
        """PenEchos Liste nur mit den Boards der Person (documentId), immer `metadataOnly=1` und nur `canvases`.

        Die Query des Browsers geht nicht weiter: `?limit` (Bibliotheksseite) lieferte sonst Seiten- und Projektzahlen
        der ganzen Ablage. Der Lernmodus fragt nur `?metadataOnly=1` (canvas-documents.js 693, persistence.js 604).
        """
        try:
            status, daten = await self._ablage_lesen(scope, "/api/canvases?metadataOnly=1")
        except _AblageWeg:
            await _json_antwort(send, 502, {"error": NICHT_ERREICHBAR})
            return
        boards = daten.get("canvases") if isinstance(daten, dict) else None
        if status != 200 or not isinstance(boards, list):
            await _json_antwort(send, 502 if status == 200 else status,
                                daten if isinstance(daten, dict) and status != 200 else {"error": NICHT_ERREICHBAR})
            return
        await _json_antwort(send, 200, {"canvases": [
            b for b in boards if isinstance(b, dict) and isinstance(b.get("documentId"), str) and b["documentId"] in eigene
        ]})

    # --- Websocket ----------------------------------------------------------------------

    async def _mitglied_frisch(self, gruppen: Gruppen, actor: str, gruppe_id: int) -> Mitgliedschaft:
        """Frisch bei LearnHouse (am 60-s-Puffer vorbei), hoechstens `pruef_frist_s` lang.

        Gruppen ist synchron (httpx); im Thread, damit der Ereignisstrang nie blockiert. Laeuft die Frist ab, gilt das
        wie ein LearnHouse-Ausfall (UebersichtWeg); der Thread endet spaetestens mit den httpx-Timeouts.
        """
        try:
            return await asyncio.wait_for(asyncio.to_thread(gruppen.mitglied, actor, gruppe_id, frisch=True),
                                          self._pruef_frist_s)
        except asyncio.TimeoutError:
            log.warning("Gruppen-Mitgliedschaft: Frist abgelaufen")
            raise UebersichtWeg() from None

    async def _abgleich_ziel(self, scope: Scope, send: Send) -> tuple[str, tuple[str, int, float]] | None:
        """Ziel (Pfad und neue Query) fuer `/api/abgleich` und (actor, gruppe_id, Pruefbeginn); None = abgelehnt.

        Fail-closed: ohne Gruppen-Einstellung, ausser dem exakten Rohpfad, ohne Kennung oder ohne gueltige
        `flaeche=g-<id>` und `fenster` ist 1008 (Solo verbindet sich nie mit dem Abgleich). Die Mitgliedschaft gilt fuer
        jede Kennung, auch den Inhaber, und wird beim Verbinden frisch gefragt: wer gerade entfernt wurde, kommt auch
        im Pufferfenster nicht wieder hinein. Ist LearnHouse weg, reicht eine gemerkte Mitgliedschaft, sonst 1013.

        Abgelehnt wird nach dem Annehmen (`_ablehnen`): vor dem Annehmen machte uvicorn daraus HTTP 403, der Browser
        saehe 1006 und verbaende sich endlos neu. 1008 (mit Text) beendet die Gruppe in der Flaeche, 1013 nicht.
        """
        state = scope.get("state")
        actor = state.get("actor_id") if isinstance(state, dict) else None
        anfrage = abgleich_anfrage(scope.get("query_string") or b"")
        gruppen = self._gruppen
        if (gruppen is None or not self._abgleich_schluessel or _rohpfad(scope) != ABGLEICH_PFAD
                or not isinstance(actor, str) or not actor or anfrage is None):
            await _ablehnen(send, 1008, GRUPPE_UNGUELTIG)
            return None
        gruppe_id, fenster = anfrage
        beginn = self._monoton()
        mitgliedschaft: Mitgliedschaft | None
        try:
            mitgliedschaft = await self._mitglied_frisch(gruppen, actor, gruppe_id)
        except KeinMitglied:
            await _ablehnen(send, 1008, KEIN_MITGLIED)
            return None
        except UebersichtWeg:
            mitgliedschaft = gruppen.gemerkt(actor, gruppe_id)
            if mitgliedschaft is None:
                await _ablehnen(send, 1013, GRUPPE_WEG)
                return None
        flaeche, person = flaeche_der_gruppe(gruppe_id), person_kennung(actor)
        schein = ausweis(self._abgleich_schluessel, flaeche, person, mitgliedschaft.anzeigename,
                         int(self._uhr()) + AUSWEIS_GUELTIG_S)
        query = urlencode([("flaeche", flaeche), ("person", person), ("fenster", fenster), ("ausweis", schein)])
        return f"{ABGLEICH_PFAD}?{query}", (actor, gruppe_id, beginn)

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
        if self._eingeschraenkt(scope) and not person_ws_erlaubt(scope):
            await send({"type": "websocket.close", "code": 1008})
            return
        ziel = _pfad_und_query(scope)
        gruppe: tuple[str, int, float] | None = None  # (actor, gruppe_id, Pruefbeginn): wird nachgeprueft
        if ist_abgleich_pfad(scope):
            abgleich = await self._abgleich_ziel(scope, send)
            if abgleich is None:
                return  # schon geschlossen
            ziel, gruppe = abgleich
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
                self._ws_basis + ziel, origin=self._basis, **{_WS_KOPF_ARGUMENT: kopf},
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
                    if closed.is_set():  # nach dem Schliessen (z. B. entfernte Person) geht nichts mehr hoch
                        return
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
                if closed.is_set():
                    return
                if isinstance(m, str):
                    await send({"type": "websocket.send", "text": m})
                else:
                    await send({"type": "websocket.send", "bytes": bytes(m)})

        async def nachpruefen(gruppen: Gruppen, actor: str, gruppe_id: int, beginn: float) -> None:
            """Frisch nachfragen; ist die Person kein Mitglied mehr, schliessen (Review-Fokus 3).

            Fester Takt ab dem Beginn der letzten Pruefung (monoton), jede Pruefung hoechstens `pruef_frist_s`: eine
            Entfernung nach dem Beginn einer Pruefung schliesst spaetestens `nachpruefen_s + pruef_frist_s` (60 s) danach.
            """
            naechste = beginn
            while True:
                naechste = max(naechste + self._nachpruefen_s, self._monoton())
                await self._schlafen(max(0.0, naechste - self._monoton()))
                try:
                    await self._mitglied_frisch(gruppen, actor, gruppe_id)
                except KeinMitglied:
                    closed.set()
                    await send({"type": "websocket.close", "code": 1008, "reason": ZUGANG_WEG})
                    return
                except UebersichtWeg:
                    continue  # LearnHouse weg: die offene Verbindung bleibt (Gruppen protokolliert den Fehlertyp)

        aufgaben = [asyncio.ensure_future(hoch()), asyncio.ensure_future(runter())]
        if gruppe is not None and self._gruppen is not None:
            aufgaben.append(asyncio.ensure_future(nachpruefen(self._gruppen, *gruppe)))
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
