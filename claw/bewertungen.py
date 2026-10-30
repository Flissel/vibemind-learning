"""Kursbewertungen: Telegram-Melder und signierter Link zur Uebersichtsseite.

Der Melder fragt die Inhaber-Routen von Learning nach neuen Bewertungen, schickt je eine
Nachricht und markiert nur die erfolgreich gesendeten als gemeldet. Keine actor_id, kein
Name und keine Adresse einer Person erscheint in der Nachricht.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from . import werkzeuge
from .freigabe import handy_basis
from .werkzeuge import ROLE_AUTHOR, _telegram_senden

logger = logging.getLogger(__name__)

PREFIX = "/bewertungen/"
GUELTIG_SEKUNDEN = 30 * 24 * 3600
_EMOJI = {"gut": "😃", "mittel": "🙂", "schlecht": "😕"}
_SERVICE_KEY_ENV = "LEARNING_VIBEMIND_SERVICE_KEY"
_PORT_ENV = "LEARNING_CLAW_MCP_PORT"


def _schluessel() -> bytes:
    roh = os.environ.get(_SERVICE_KEY_ENV, "").strip()
    return hashlib.sha256(("bewertungen:" + roh).encode("utf-8")).digest()


def _signatur(ablauf: int) -> str:
    return hmac.new(_schluessel(), str(ablauf).encode("ascii"), hashlib.sha256).hexdigest()


def token_erzeugen(jetzt: float | None = None) -> str:
    ablauf = int((time.time() if jetzt is None else jetzt) + GUELTIG_SEKUNDEN)
    roh = f"{ablauf}.{_signatur(ablauf)}"
    return base64.urlsafe_b64encode(roh.encode("ascii")).decode("ascii").rstrip("=")


def token_gueltig(token: str, jetzt: float | None = None) -> bool:
    try:
        roh = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii")
        ablauf_text, signatur = roh.split(".", 1)
        ablauf = int(ablauf_text)
    except (ValueError, UnicodeError):
        return False
    erwartet = _signatur(ablauf).encode("ascii")
    if not hmac.compare_digest(signatur.encode("ascii", "replace"), erwartet):
        return False
    return (time.time() if jetzt is None else jetzt) <= ablauf


def _ursprung() -> str:
    basis = handy_basis()
    if basis is not None:
        return basis[0]
    port = os.environ.get(_PORT_ENV, "").strip() or "8139"
    return f"http://127.0.0.1:{port}"


def nachricht(b: dict[str, Any], link: str) -> str:
    emoji = _EMOJI.get(str(b.get("smiley")), "")
    stichworte = [str(s) for s in b.get("stichworte") or []]
    teil = ", ".join(f"„{s}“" for s in stichworte) if stichworte else "ohne Stichworte"
    v = b.get("verteilung") or {}
    gut, mittel, schlecht = (int(v.get(k) or 0) for k in ("gut", "mittel", "schlecht"))
    zusatz = " (geändert)" if b.get("geaendert") else ""
    return (
        f"Kursbewertung · {b.get('kurs_titel') or ''}{zusatz}\n"
        f"{emoji} – {teil}\n"
        f"Bisher {gut + mittel + schlecht} Bewertungen: {gut} 😃 · {mittel} 🙂 · {schlecht} 😕\n"
        f"Übersicht: {link}"
    )


def melde_runde(
    client: Any, senden: Callable[[str], bool] = _telegram_senden, jetzt: float | None = None
) -> int:
    """Meldet alle neuen Bewertungen; gibt die Zahl der gemeldeten zurueck, wirft nie."""
    try:
        neu = client.get("/kursbewertungen/neu", roles=ROLE_AUTHOR).get("bewertungen") or []
        if not neu:
            return 0
        adresse = f"{_ursprung()}{PREFIX}{token_erzeugen(jetzt)}"
        erledigt: list[str] = []
        for b in neu:
            try:
                if senden(nachricht(b, adresse)):
                    erledigt.append(str(b["id"]))
            except Exception as error:  # noqa: BLE001 - one broken send must not stop the round
                logger.warning("Bewertung konnte nicht gemeldet werden: %s", type(error).__name__)
        if erledigt:
            client.post("/kursbewertungen/gemeldet", {"ids": erledigt}, roles=ROLE_AUTHOR)
        return len(erledigt)
    except Exception as error:  # noqa: BLE001 - the reporter never takes the sidecar down
        logger.warning("Bewertungs-Melder-Runde fehlgeschlagen: %s", type(error).__name__)
        return 0


def _runde_mit_neuem_client() -> int:
    try:
        with werkzeuge._client() as client:
            return melde_runde(client)
    except Exception as error:  # noqa: BLE001
        logger.warning("Bewertungs-Melder ohne Client: %s", type(error).__name__)
        return 0


def starte_melder(intervall_s: int = 300) -> threading.Thread:
    def schleife() -> None:
        while True:
            _runde_mit_neuem_client()
            time.sleep(intervall_s)

    faden = threading.Thread(target=schleife, name="bewertungen-melder", daemon=True)
    faden.start()
    return faden
