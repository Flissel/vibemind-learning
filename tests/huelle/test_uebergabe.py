"""Uebergabe aus LearnHouse und Personen-Cookie der Huelle: Format, Pruefvektor, Label-Trennung."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from spaces.learning.huelle.person import PERSON_COOKIE, PERSON_GUELTIG, person_aus_cookie, person_cookie
from spaces.learning.huelle.uebergabe import uebergabe_lesen
from spaces.learning.huelle.zugang import token_erzeugen

VEKTOR = json.loads((Path(__file__).parent / "uebergabe_vektor.json").read_text(encoding="utf-8"))
SCHLUESSEL = "test-only-huelle-schluessel-not-a-secret-0123456789"
KURS = "3f0c5c1e-7a52-4c1b-9d2e-0a6b8f4e2d10"
JETZT = 1_800_000_000


def _b64(daten: bytes) -> str:
    return base64.urlsafe_b64encode(daten).decode().rstrip("=")


def _signiert(label: bytes, schluessel: str, inhalt: str) -> str:
    """Token im LearnHouse-Format unter einem beliebigen Label (fuer Gegenproben)."""
    payload = _b64(inhalt.encode())
    key = hashlib.sha256(label + schluessel.encode()).digest()
    return f"{payload}.{hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:48]}"


def _uebergabe(inhalt: str, schluessel: str = SCHLUESSEL) -> str:
    return _signiert(b"uebergabe:", schluessel, inhalt)


def test_uebergabe_vektor_wie_learnhouse() -> None:
    actor, kurs = uebergabe_lesen(VEKTOR["key"], VEKTOR["token"], jetzt=VEKTOR["now"])
    assert (actor, kurs) == (VEKTOR["actor_id"], VEKTOR["course_id"])
    # Der Vektor ist 120 s gueltig, nicht laenger.
    assert uebergabe_lesen(VEKTOR["key"], VEKTOR["token"], jetzt=VEKTOR["now"] + 120)[0] == VEKTOR["actor_id"]
    with pytest.raises(ValueError, match="^abgelaufen$"):
        uebergabe_lesen(VEKTOR["key"], VEKTOR["token"], jetzt=VEKTOR["now"] + 121)
    with pytest.raises(ValueError, match="^ungueltig$"):
        uebergabe_lesen(VEKTOR["key"] + "x", VEKTOR["token"], jetzt=VEKTOR["now"])


def test_uebergabe_ohne_kurs() -> None:
    token = _uebergabe(f"person-a||{JETZT + 60}")
    assert uebergabe_lesen(SCHLUESSEL, token, jetzt=JETZT) == ("person-a", None)


@pytest.mark.parametrize("token", [
    "", ".", "muell", "abc.", ".abc", "é", "é.é", "a\udcff.b", "\x00.\x00",
    "bGgtMS1wZXJzb24tYXx8MTgwMDAwMDEyMA.é" + "0" * 47,
])
def test_uebergabe_kaputt_ist_ungueltig_nie_ausnahme(token: str) -> None:
    with pytest.raises(ValueError, match="^ungueltig$"):
        uebergabe_lesen(SCHLUESSEL, token, jetzt=JETZT)


@pytest.mark.parametrize("inhalt", [
    f"person-a\n|{KURS}|{JETZT + 60}",          # Zeilenende nach der Kennung
    f"person-a|{KURS}\n|{JETZT + 60}",          # Zeilenende nach dem Kurs
    f"-person-a|{KURS}|{JETZT + 60}",           # Kennung beginnt nicht alphanumerisch
    f"person/a|{KURS}|{JETZT + 60}",            # Zeichen ausserhalb des Musters
    f"|{KURS}|{JETZT + 60}",                    # leere Kennung
    f"person-a|{KURS.upper()}|{JETZT + 60}",    # keine kanonische UUID
    f"person-a|{KURS.replace('-', '')}|{JETZT + 60}",
    f"person-a|kein-kurs|{JETZT + 60}",
    f"person-a|{KURS}|bald",                    # Ablauf keine Zahl
    f"person-a|{KURS}",                         # zu wenige Teile
    f"person-a|{KURS}|{JETZT + 60}|x",          # zu viele Teile
    f"pérson-a|{KURS}|{JETZT + 60}",            # Nicht-ASCII in der Kennung
])
def test_uebergabe_inhalt_streng_geprueft(inhalt: str) -> None:
    with pytest.raises(ValueError, match="^ungueltig$"):
        uebergabe_lesen(SCHLUESSEL, _uebergabe(inhalt), jetzt=JETZT)


def test_person_cookie_hin_und_zurueck() -> None:
    assert PERSON_COOKIE == "huelle_person" and PERSON_GUELTIG == 12 * 3600
    wert = person_cookie(SCHLUESSEL, "person-a", jetzt=JETZT)
    assert person_aus_cookie(SCHLUESSEL, wert, jetzt=JETZT) == "person-a"
    assert person_aus_cookie(SCHLUESSEL, wert, jetzt=JETZT + PERSON_GUELTIG) == "person-a"
    assert person_aus_cookie(SCHLUESSEL, wert, jetzt=JETZT + PERSON_GUELTIG + 1) is None
    assert person_aus_cookie("x" * 40, wert, jetzt=JETZT) is None
    for kaputt in ("", "muell", "é", "é.é", wert + "0", wert[:-1] + "é", "a\udcff.b"):
        assert person_aus_cookie(SCHLUESSEL, kaputt, jetzt=JETZT) is None
    with pytest.raises(ValueError):
        person_cookie(SCHLUESSEL, "person-a\n", jetzt=JETZT)


def test_person_token_ist_kein_uebergabe_token() -> None:
    # Personen-Cookie der Huelle gilt nicht als Uebergabe ...
    cookie = person_cookie(SCHLUESSEL, "person-a", jetzt=JETZT)
    with pytest.raises(ValueError, match="^ungueltig$"):
        uebergabe_lesen(SCHLUESSEL, cookie, jetzt=JETZT)
    # ... und eine Uebergabe (auch eine mit Personen-Cookie-Inhalt) nicht als Personen-Cookie.
    assert person_aus_cookie(SCHLUESSEL, _uebergabe(f"person-a|{KURS}|{JETZT + 60}"), jetzt=JETZT) is None
    assert person_aus_cookie(SCHLUESSEL, _uebergabe(f"person-a|{JETZT + 60}"), jetzt=JETZT) is None
    # LearnHouses Person-Token (Label "person:", gleiches Format wie das Cookie) gilt unter keinem Label der Huelle.
    fremd = _signiert(b"person:", SCHLUESSEL, f"person-a|{JETZT + 60}")
    assert person_aus_cookie(SCHLUESSEL, fremd, jetzt=JETZT) is None
    with pytest.raises(ValueError, match="^ungueltig$"):
        uebergabe_lesen(SCHLUESSEL, _signiert(b"person:", SCHLUESSEL, f"person-a|{KURS}|{JETZT + 60}"), jetzt=JETZT)
    # Das alte Zugangscookie (Label "huelle:") ist weder das eine noch das andere.
    zugang = token_erzeugen(SCHLUESSEL, jetzt=JETZT)
    assert person_aus_cookie(SCHLUESSEL, zugang, jetzt=JETZT) is None
    with pytest.raises(ValueError, match="^ungueltig$"):
        uebergabe_lesen(SCHLUESSEL, zugang, jetzt=JETZT)
