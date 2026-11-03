"""Zugangstoken der Huelle: HMAC-SHA256, 30 Tage, gleicher Aufbau wie claw/bewertungen."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

COOKIE = "huelle_zugang"
GUELTIG_SEKUNDEN = 30 * 24 * 3600


def _signatur(schluessel: str, ablauf: int) -> str:
    key = hashlib.sha256(("huelle:" + schluessel).encode("utf-8")).digest()
    return hmac.new(key, str(ablauf).encode("ascii"), hashlib.sha256).hexdigest()


def token_erzeugen(schluessel: str, jetzt: float | None = None) -> str:
    ablauf = int((time.time() if jetzt is None else jetzt) + GUELTIG_SEKUNDEN)
    roh = f"{ablauf}.{_signatur(schluessel, ablauf)}"
    return base64.urlsafe_b64encode(roh.encode("ascii")).decode("ascii").rstrip("=")


def token_gueltig(schluessel: str, token: str, jetzt: float | None = None) -> bool:
    try:
        roh = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii")
        ablauf_text, signatur = roh.split(".", 1)
        ablauf = int(ablauf_text)
    except (ValueError, UnicodeError):
        return False
    erwartet = _signatur(schluessel, ablauf).encode("ascii")
    if not hmac.compare_digest(signatur.encode("ascii", "replace"), erwartet):
        return False
    return (time.time() if jetzt is None else jetzt) <= ablauf
