"""KiBewerter: Client fuer den Bewertungsendpunkt der Lernflaeche (httpx MockTransport, nie ein echter Port)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx
import pytest

from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.lernflaeche_bewertung import KiBewerter

SCHLUESSEL = "geheim-" + "s" * 40
URL = "http://flaeche.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
MUSTER = "MUSTERLOESUNG-NICHT-LOGGEN"
DATEN = {"attempt_id": "a1", "art": "rubric", "musterloesung": MUSTER,
         "kriterien": [{"criterion_id": "c1", "description": "d", "points": 10}]}
GUT = {"grader": "ki_flaeche", "kriterien": [{"criterion_id": "c1", "punkte": 10, "rueckmeldung": "ok"}],
       "rueckmeldung": "gut"}


def _bewerter(handler: Any, schluessel: str = SCHLUESSEL, **kw: Any) -> KiBewerter:
    return KiBewerter(URL, schluessel, transport=httpx.MockTransport(handler), **kw)


def test_ki_bewerter_schickt_schluessel_und_daten() -> None:
    gesehen: list[httpx.Request] = []

    def handler(anfrage: httpx.Request) -> httpx.Response:
        gesehen.append(anfrage)
        return httpx.Response(200, json=GUT)

    bewerter = _bewerter(handler)
    assert bewerter.bewerten(PNG, DATEN) == GUT
    (anfrage,) = gesehen
    assert anfrage.method == "POST" and anfrage.url == httpx.URL(f"{URL}/api/lernflaeche/bewerten")
    assert anfrage.headers["X-Lernflaeche-Bewertung"] == SCHLUESSEL
    assert json.loads(anfrage.content) == {"bild_png_base64": base64.b64encode(PNG).decode("ascii"), "daten": DATEN}
    assert anfrage.extensions["timeout"]["read"] == 120


def _timeout(anfrage: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("zu langsam", request=anfrage)


@pytest.mark.parametrize("handler", [
    lambda a: httpx.Response(500, text=MUSTER),
    lambda a: httpx.Response(403, json={"fehler": MUSTER}),
    _timeout,
    lambda a: httpx.Response(200, content=b"{kaputt " + MUSTER.encode()),
    lambda a: httpx.Response(200, json=["kein", "objekt"]),
    lambda a: httpx.Response(200, json={"grader": "server"}),
], ids=["500", "403", "timeout", "kaputtes-json", "kein-objekt", "falscher-bewerter"])
def test_ki_bewerter_none_bei_fehler(handler: Any, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        assert _bewerter(handler).bewerten(PNG, DATEN) is None
    log = caplog.text
    assert MUSTER not in log and SCHLUESSEL not in log and "kriterien" not in log


@pytest.mark.parametrize("schluessel", ["", "   "])
def test_ki_bewerter_ohne_schluessel_oder_url_ruft_nicht(schluessel: str) -> None:
    def handler(anfrage: httpx.Request) -> httpx.Response:
        raise AssertionError("kein HTTP-Aufruf erwartet")

    bewerter = _bewerter(handler, schluessel)
    assert bewerter.aktiv is False and bewerter.bewerten(PNG, DATEN) is None
    ohne_url = KiBewerter("", SCHLUESSEL, transport=httpx.MockTransport(handler))
    assert ohne_url.aktiv is False and ohne_url.bewerten(PNG, DATEN) is None


def test_ki_bewerter_verbindungsfehler_ist_none() -> None:
    def handler(anfrage: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("weg", request=anfrage)

    assert _bewerter(handler).bewerten(PNG, DATEN) is None


_BASIS = {
    "LEARNING_HUELLE_BASIS": "https://lernen.example.test",
    "LEARNING_HUELLE_FLAECHE_BASIS": "https://lernen.example.test:8443",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNHOUSE_LEARNING_SERVICE_KEY": "k" * 40,
    "LEARNING_HUELLE_ACTOR": "lh-1-test",
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": "11111111-1111-4111-8111-111111111111", "titel": "T"}]),
}


def test_einstellung_bewertung_optional_mit_vorgabe() -> None:
    e = einstellung_lesen(_BASIS)
    assert e.bewertung_schluessel == "" and e.lernflaeche_url == "http://127.0.0.1:3888"
    e = einstellung_lesen({**_BASIS, "LEARNING_HUELLE_BEWERTUNG_SCHLUESSEL": " " + "b" * 32 + "\r\n",
                           "LEARNING_HUELLE_LERNFLAECHE_URL": "http://127.0.0.1:3889/"})
    assert e.bewertung_schluessel == "b" * 32 and e.lernflaeche_url == "http://127.0.0.1:3889"
    assert "b" * 32 not in repr(e)


def test_einstellung_bewertungsschluessel_zu_kurz() -> None:
    with pytest.raises(RuntimeError, match="LEARNING_HUELLE_BEWERTUNG_SCHLUESSEL ist zu kurz") as fehler:
        einstellung_lesen({**_BASIS, "LEARNING_HUELLE_BEWERTUNG_SCHLUESSEL": "kurz"})
    assert "kurz" not in str(fehler.value).replace("zu kurz", "")
