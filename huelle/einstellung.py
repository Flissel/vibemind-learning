"""Einstellung der Lern-Huelle, gelesen aus der Umgebung.

Fehler nennen nur den Namen der Variable, nie ihren Wert.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MIN_SCHLUESSEL = 32

HOST = "LEARNING_HUELLE_HOST"
PORT = "LEARNING_HUELLE_PORT"
BASIS = "LEARNING_HUELLE_BASIS"
PENECHO_URL = "LEARNING_HUELLE_PENECHO_URL"
PENECHO_STATE = "LEARNING_HUELLE_PENECHO_STATE"
PENECHO_MCP_URL = "LEARNING_HUELLE_PENECHO_MCP_URL"
MCP_URL = "LEARNING_HUELLE_MCP_URL"
API_URL = "LEARNING_HUELLE_API_URL"
SCHLUESSEL = "LEARNHOUSE_LEARNING_SERVICE_KEY"
ACTOR = "LEARNING_HUELLE_ACTOR"
KURSE = "LEARNING_HUELLE_KURSE"
# Zweite Herkunft nur fuer PenEcho (Ruling J): PenEcho-Widgets laufen mit der Herkunft, die PenEcho
# ausliefert, und duerfen die Seite und ihre API nicht erreichen.
FLAECHE_PORT = "LEARNING_HUELLE_FLAECHE_PORT"
FLAECHE_BASIS = "LEARNING_HUELLE_FLAECHE_BASIS"
PENECHO_PAKET = "LEARNING_HUELLE_PENECHO_PAKET"
# Optionaler Wissensordner: dort legt die Huelle die Textfassung der Zusammenfassung ab (leer = aus).
ZUSAMMENFASSUNG_ORDNER = "LEARNING_HUELLE_ZUSAMMENFASSUNG_ORDNER"
# Optional (Teil B): LearnHouse-API fuer Uebergabe und Kurse je Person. Leer = nur der Notweg
# (Zugangscookie mit LEARNING_HUELLE_ACTOR und LEARNING_HUELLE_KURSE), genau wie bisher.
LEARNHOUSE_URL = "LEARNING_HUELLE_LEARNHOUSE_URL"
# Optional (Teil C): Abgleichdienst der Gruppenflaechen (http-Basis fuer /platz und /schutz) und sein Schluessel
# (signiert Ausweise und Dienstkopf). Gruppen gibt es nur mit beiden und mit LEARNING_HUELLE_LEARNHOUSE_URL.
ABGLEICH_URL = "LEARNING_HUELLE_ABGLEICH_URL"
ABGLEICH_SCHLUESSEL = "LEARNING_HUELLE_ABGLEICH_SCHLUESSEL"
# Optional (Teil C): wie viele freie KI-Anfragen (/api/ai/command) der Personen gleichzeitig laufen (Vorgabe 3).
KI_GLEICHZEITIG = "LEARNING_HUELLE_KI_GLEICHZEITIG"
KI_GLEICHZEITIG_VORGABE = 3
# Optional (KI-Tutor Teil 2): Bewertung der Abgabe durch die KI der Lernflaeche (Server zu Server). Ohne Schluessel
# bewertet der Server wie bisher. Derselbe Wert steht als LERNFLAECHE_BEWERTUNG_SCHLUESSEL in penecho.env.
LERNFLAECHE_URL = "LEARNING_HUELLE_LERNFLAECHE_URL"
LERNFLAECHE_URL_VORGABE = "http://127.0.0.1:3888"
BEWERTUNG_SCHLUESSEL = "LEARNING_HUELLE_BEWERTUNG_SCHLUESSEL"


@dataclass(frozen=True)
class Kurs:
    course_id: str
    titel: str


@dataclass(frozen=True)
class Einstellung:
    host: str
    port: int
    basis: str
    penecho_url: str
    penecho_state: str
    mcp_url: str
    api_url: str
    dienst_schluessel: str
    actor_id: str
    kurse: tuple[Kurs, ...]
    flaeche_port: int
    flaeche_basis: str
    penecho_paket: str
    penecho_mcp_url: str
    zusammenfassung_ordner: str = ""
    learnhouse_url: str = ""
    abgleich_url: str = ""
    abgleich_schluessel: str = field(default="", repr=False)
    ki_gleichzeitig: int = KI_GLEICHZEITIG_VORGABE
    lernflaeche_url: str = LERNFLAECHE_URL_VORGABE
    bewertung_schluessel: str = field(default="", repr=False)

    @property
    def gruppen_aktiv(self) -> bool:
        """Gruppenflaechen (Teil C) nur mit LearnHouse, Abgleichdienst und dessen Schluessel; sonst 404/1008."""
        return bool(self.learnhouse_url and self.abgleich_url and self.abgleich_schluessel)

    @property
    def basis_host(self) -> str:
        return basis_aufteilen(self.basis, BASIS)[1]

    @property
    def erlaubte_hosts(self) -> frozenset[str]:
        """Hosts des Seiten-Ports (Seite, API, Zugang)."""
        return frozenset({self.basis_host, f"127.0.0.1:{self.port}"})

    @property
    def flaechen_hosts(self) -> frozenset[str]:
        """Hosts des Flaechen-Ports (nur PenEcho)."""
        return frozenset({basis_aufteilen(self.flaeche_basis, FLAECHE_BASIS)[1], f"127.0.0.1:{self.flaeche_port}"})

    @property
    def seiten_herkuenfte(self) -> tuple[str, str]:
        return (self.basis, f"http://127.0.0.1:{self.port}")

    @property
    def flaechen_herkuenfte(self) -> tuple[str, str]:
        return (self.flaeche_basis, f"http://127.0.0.1:{self.flaeche_port}")


def basis_aufteilen(roh: str, name: str = BASIS) -> tuple[str, str]:
    """`(origin, host)` einer reinen https-Herkunft; der Port 443 entfaellt."""
    text = roh.strip().rstrip("/")
    try:
        teile = urlsplit(text)
        port = teile.port
    except ValueError:
        raise RuntimeError(f"{name} ist keine https-Herkunft") from None
    if (
        teile.scheme.lower() != "https" or not teile.hostname or teile.path
        or teile.query or teile.fragment or "@" in teile.netloc
    ):
        raise RuntimeError(f"{name} ist keine https-Herkunft")
    host = teile.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port not in (None, 443):
        host = f"{host}:{port}"
    return f"https://{host}", host


def _pflicht(env: Mapping[str, str], name: str) -> str:
    wert = (env.get(name) or "").strip()
    if not wert:
        raise RuntimeError(f"{name} fehlt")
    return wert


def _kurse(roh: str) -> tuple[Kurs, ...]:
    try:
        daten = json.loads(roh)
        kurse = tuple(Kurs(str(uuid.UUID(str(e["course_id"]))), str(e["titel"])) for e in daten)
    except (ValueError, TypeError, KeyError):
        raise RuntimeError(f"{KURSE} ist keine gueltige Kursliste") from None
    if not kurse:
        raise RuntimeError(f"{KURSE} braucht mindestens einen Eintrag")
    return kurse


def _port(env: Mapping[str, str], name: str, vorgabe: int) -> int:
    try:
        port = int((env.get(name) or "").strip() or str(vorgabe))
    except ValueError:
        raise RuntimeError(f"{name} ist keine Zahl") from None
    if not 0 < port < 65536:
        raise RuntimeError(f"{name} liegt ausserhalb des Bereichs")
    return port


def _http_basis(env: Mapping[str, str], name: str) -> str:
    """Leer oder eine http(s)-Basisadresse ohne Query, Fragment und Zugangsdaten (ohne `/` am Ende)."""
    roh = (env.get(name) or "").strip().rstrip("/")
    if not roh:
        return ""
    try:
        teile = urlsplit(roh)
        teile.port  # noqa: B018 - prueft die Portangabe
    except ValueError:
        raise RuntimeError(f"{name} ist keine http(s)-Adresse") from None
    if (teile.scheme.lower() not in ("http", "https") or not teile.hostname or teile.query or teile.fragment
            or "@" in teile.netloc):
        raise RuntimeError(f"{name} ist keine http(s)-Adresse")
    return roh


def _learnhouse_url(env: Mapping[str, str]) -> str:
    return _http_basis(env, LEARNHOUSE_URL)


def _abgleich_schluessel(env: Mapping[str, str]) -> str:
    """Leer oder mindestens MIN_SCHLUESSEL Zeichen; Leerraum am Rand (auch CRLF der Env-Dateien) zaehlt nicht."""
    wert = (env.get(ABGLEICH_SCHLUESSEL) or "").strip()
    if wert and len(wert) < MIN_SCHLUESSEL:
        raise RuntimeError(f"{ABGLEICH_SCHLUESSEL} ist zu kurz")
    return wert


def _bewertung_schluessel(env: Mapping[str, str]) -> str:
    """Leer (Bewertung durch die Lernflaeche aus) oder mindestens MIN_SCHLUESSEL Zeichen."""
    wert = (env.get(BEWERTUNG_SCHLUESSEL) or "").strip()
    if wert and len(wert) < MIN_SCHLUESSEL:
        raise RuntimeError(f"{BEWERTUNG_SCHLUESSEL} ist zu kurz")
    return wert


def _ki_gleichzeitig(env: Mapping[str, str]) -> int:
    """Ganze Zahl >= 1; leer, ungueltig oder <= 0 ergibt die Vorgabe (geloggt wird nur die Art des Fehlers)."""
    roh = (env.get(KI_GLEICHZEITIG) or "").strip()
    if not roh:
        return KI_GLEICHZEITIG_VORGABE
    try:
        wert = int(roh)
    except ValueError:
        log.warning("%s ist keine ganze Zahl; Vorgabe %d gilt", KI_GLEICHZEITIG, KI_GLEICHZEITIG_VORGABE)
        return KI_GLEICHZEITIG_VORGABE
    if wert <= 0:
        log.warning("%s ist nicht positiv; Vorgabe %d gilt", KI_GLEICHZEITIG, KI_GLEICHZEITIG_VORGABE)
        return KI_GLEICHZEITIG_VORGABE
    return wert


def _penecho_paket_vorgabe(env: Mapping[str, str]) -> str:
    """Die PenEcho-Installation der PC-Einrichtung (Plan, Task 10)."""
    return os.path.join(env.get("LOCALAPPDATA") or "", "VibeMind", "penecho", "node_modules", "penecho")


def einstellung_lesen(env: Mapping[str, str] | None = None) -> Einstellung:
    env = os.environ if env is None else env
    port = _port(env, PORT, 8150)
    flaeche_port = _port(env, FLAECHE_PORT, 8151)
    if flaeche_port == port:
        raise RuntimeError(f"{FLAECHE_PORT} muss sich von {PORT} unterscheiden")
    basis = basis_aufteilen(_pflicht(env, BASIS), BASIS)[0]
    flaeche_basis = basis_aufteilen(_pflicht(env, FLAECHE_BASIS), FLAECHE_BASIS)[0]
    # Gleicher Host, anderer Port: das Zugangscookie gilt je Host (Ports zaehlen nicht) fuer beide,
    # die Herkuenfte bleiben getrennt.
    if flaeche_basis == basis or urlsplit(flaeche_basis).hostname != urlsplit(basis).hostname:
        raise RuntimeError(f"{FLAECHE_BASIS} braucht denselben Host wie {BASIS}, aber einen anderen Port")
    schluessel = _pflicht(env, SCHLUESSEL)
    if len(schluessel) < MIN_SCHLUESSEL:
        raise RuntimeError(f"{SCHLUESSEL} ist zu kurz")
    actor = _pflicht(env, ACTOR)
    if not _ACTOR.match(actor):
        raise RuntimeError(f"{ACTOR} hat ein ungueltiges Muster")
    return Einstellung(
        host=(env.get(HOST) or "").strip() or "127.0.0.1",
        port=port,
        basis=basis,
        penecho_url=(env.get(PENECHO_URL) or "").strip() or "http://127.0.0.1:3888",
        penecho_state=_pflicht(env, PENECHO_STATE),
        mcp_url=(env.get(MCP_URL) or "").strip() or "http://127.0.0.1:8091/mcp",
        api_url=(env.get(API_URL) or "").strip() or "http://127.0.0.1:8090",
        dienst_schluessel=schluessel,
        actor_id=actor,
        kurse=_kurse(_pflicht(env, KURSE)),
        flaeche_port=flaeche_port,
        flaeche_basis=flaeche_basis,
        penecho_paket=(env.get(PENECHO_PAKET) or "").strip() or _penecho_paket_vorgabe(env),
        penecho_mcp_url=(env.get(PENECHO_MCP_URL) or "").strip() or "https://127.0.0.1:3922/mcp",
        zusammenfassung_ordner=(env.get(ZUSAMMENFASSUNG_ORDNER) or "").strip(),
        learnhouse_url=_learnhouse_url(env),
        abgleich_url=_http_basis(env, ABGLEICH_URL),
        abgleich_schluessel=_abgleich_schluessel(env),
        ki_gleichzeitig=_ki_gleichzeitig(env),
        lernflaeche_url=_http_basis(env, LERNFLAECHE_URL) or LERNFLAECHE_URL_VORGABE,
        bewertung_schluessel=_bewertung_schluessel(env),
    )
