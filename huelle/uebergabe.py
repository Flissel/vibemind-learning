"""Uebergabe aus LearnHouse: kurzlebiges Token, das einen Kurs auf der Lern-Huelle oeffnet.

Gegenstueck zu LearnHouse `services/learning/uebergabe.py` (Fork). LearnHouse signiert
`<actor_id>|<course_id oder leer>|<ablauf>` mit dem gemeinsamen Dienstschluessel
(`LEARNHOUSE_LEARNING_SERVICE_KEY`) unter dem eigenen Ableitungslabel `uebergabe:`:
base64url ohne Auffuellung, `.`, HMAC-SHA256 hex auf 48 Zeichen gekuerzt. Ein Token eines
anderen Labels (`person:`, `huelle-person:`, `huelle:`) gilt hier nie. Gueltig 120 s.

Verglichen werden Bytes: jedes Zeichen aus einer URL (auch Nicht-ASCII, auch einzelne
Surrogate) ergibt hoechstens `ungueltig`, nie eine Ausnahme.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from uuid import UUID

_LABEL = b"uebergabe:"
# Wie LearnHouse `person_token._ACTOR`, aber immer mit fullmatch (kein Zeilenende nach der Kennung).
ACTOR_MUSTER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def als_bytes(text: str) -> bytes:
    """Jede Zeichenkette aus einer URL als Bytes (auch einzelne Surrogate)."""
    return text.encode("utf-8", "surrogatepass")


def signatur(label: bytes, schluessel: str, payload: str) -> str:
    key = hashlib.sha256(label + schluessel.encode("utf-8")).digest()
    return hmac.new(key, als_bytes(payload), hashlib.sha256).hexdigest()[:48]


def signatur_passt(label: bytes, schluessel: str, token: str) -> str | None:
    """Der Payload-Teil eines Tokens mit passender Signatur unter diesem Label, sonst None."""
    payload, _, sig = token.partition(".")
    if not payload:
        return None
    if not hmac.compare_digest(signatur(label, schluessel, payload).encode("ascii"), als_bytes(sig)):
        return None
    return payload


def payload_text(payload: str) -> str:
    """Den signierten Payload dekodieren; ValueError bei allem, was kein base64url-UTF-8 ist."""
    try:
        return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
    except ValueError:  # binascii.Error, UnicodeDecodeError, Nicht-ASCII im Text
        raise ValueError("ungueltig") from None


def ist_course_id(wert: str) -> bool:
    """Nur die kanonische UUID-Schreibweise (klein, mit Bindestrichen)."""
    try:
        return str(UUID(wert)) == wert
    except ValueError:
        return False


def uebergabe_lesen(schluessel: str, token: str, jetzt: float | None = None) -> tuple[str, str | None]:
    """`(actor_id, course_id oder None)` eines gueltigen Tokens; sonst `ValueError("ungueltig"|"abgelaufen")`."""
    payload = signatur_passt(_LABEL, schluessel, token)
    if payload is None:
        raise ValueError("ungueltig")
    try:
        actor_id, course_id, ablauf_text = payload_text(payload).split("|")
        ablauf = int(ablauf_text)
    except ValueError:  # auch eine falsche Zahl von Teilen
        raise ValueError("ungueltig") from None
    if ACTOR_MUSTER.fullmatch(actor_id) is None or (course_id and not ist_course_id(course_id)):
        raise ValueError("ungueltig")
    if ablauf < (time.time() if jetzt is None else jetzt):
        raise ValueError("abgelaufen")
    return actor_id, course_id or None
