"""Personen-Cookie der Lern-Huelle: wer diese Anfrage stellt (12 h nach der Uebergabe aus LearnHouse).

Gleiches Format wie LearnHouses Person-Token (`<actor_id>|<ablauf>` base64url, `.`, HMAC-SHA256 hex
auf 48 Zeichen), aber unter dem eigenen Ableitungslabel `huelle-person:`. Ein Token eines anderen
Labels (`uebergabe:`, `person:`, `huelle:`) gilt hier nie. Der Wert besteht nur aus Cookie-sicheren
Zeichen (base64url, `.`, hex).
"""

from __future__ import annotations

import base64
import time

from .uebergabe import ACTOR_MUSTER, payload_text, signatur, signatur_passt

PERSON_COOKIE = "huelle_person"
PERSON_GUELTIG = 12 * 3600
_LABEL = b"huelle-person:"


def person_cookie(schluessel: str, actor_id: str, jetzt: float | None = None) -> str:
    if ACTOR_MUSTER.fullmatch(actor_id) is None:
        raise ValueError("ungueltige Kennung")
    ablauf = int((time.time() if jetzt is None else jetzt) + PERSON_GUELTIG)
    payload = base64.urlsafe_b64encode(f"{actor_id}|{ablauf}".encode("ascii")).decode("ascii").rstrip("=")
    return f"{payload}.{signatur(_LABEL, schluessel, payload)}"


def person_aus_cookie(schluessel: str, wert: str, jetzt: float | None = None) -> str | None:
    """Die Kennung eines gueltigen, nicht abgelaufenen Cookies; sonst None (nie eine Ausnahme)."""
    payload = signatur_passt(_LABEL, schluessel, wert)
    if payload is None:
        return None
    try:
        actor_id, ablauf_text = payload_text(payload).split("|")
        ablauf = int(ablauf_text)
    except ValueError:
        return None
    if ACTOR_MUSTER.fullmatch(actor_id) is None:
        return None
    if ablauf < (time.time() if jetzt is None else jetzt):
        return None
    return actor_id
