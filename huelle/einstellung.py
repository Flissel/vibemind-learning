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
MCP_URL = "LEARNING_HUELLE_MCP_URL"
API_URL = "LEARNING_HUELLE_API_URL"
SCHLUESSEL = "LEARNHOUSE_LEARNING_SERVICE_KEY"
ACTOR = "LEARNING_HUELLE_ACTOR"
KURSE = "LEARNING_HUELLE_KURSE"


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

    @property
    def basis_host(self) -> str:
        return basis_aufteilen(self.basis, BASIS)[1]

    @property
    def erlaubte_hosts(self) -> frozenset[str]:
        return frozenset({self.basis_host, f"127.0.0.1:{self.port}"})


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


def einstellung_lesen(env: Mapping[str, str] | None = None) -> Einstellung:
    env = os.environ if env is None else env
    try:
        port = int((env.get(PORT) or "").strip() or "8150")
    except ValueError:
        raise RuntimeError(f"{PORT} ist keine Zahl") from None
    if not 0 < port < 65536:
        raise RuntimeError(f"{PORT} liegt ausserhalb des Bereichs")
    basis = basis_aufteilen(_pflicht(env, BASIS), BASIS)[0]
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
    )
