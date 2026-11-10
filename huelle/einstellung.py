"""Einstellung der Lern-Huelle, gelesen aus der Umgebung.

Fehler nennen nur den Namen der Variable, nie ihren Wert.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

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


def _learnhouse_url(env: Mapping[str, str]) -> str:
    """Leer oder eine http(s)-Basisadresse ohne Query, Fragment und Zugangsdaten (ohne `/` am Ende)."""
    roh = (env.get(LEARNHOUSE_URL) or "").strip().rstrip("/")
    if not roh:
        return ""
    try:
        teile = urlsplit(roh)
        teile.port  # noqa: B018 - prueft die Portangabe
    except ValueError:
        raise RuntimeError(f"{LEARNHOUSE_URL} ist keine http(s)-Adresse") from None
    if (teile.scheme.lower() not in ("http", "https") or not teile.hostname or teile.query or teile.fragment
            or "@" in teile.netloc):
        raise RuntimeError(f"{LEARNHOUSE_URL} ist keine http(s)-Adresse")
    return roh


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
    )
