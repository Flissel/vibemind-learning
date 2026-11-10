"""Katalog der Huelle: Kurse je Person aus LearnHouse (Dienstroute, 60 s Puffer), Notweg aus der Umgebung."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from spaces.learning.huelle.einstellung import Kurs, einstellung_lesen
from spaces.learning.huelle.katalog import (
    KEIN_ZUGANG_MEHR, PUFFER_S, TIMEOUT_S, UEBERSICHT_WEG, Katalog, KeinZugang, UebersichtWeg,
)

KURS = "11111111-1111-4111-8111-111111111111"
KURS_B = "55555555-5555-4555-8555-555555555555"
SCHLUESSEL = "k" * 40
LH = "http://learnhouse.test"

BASIS = {
    "LEARNING_HUELLE_BASIS": "https://lernen.example.test",
    "LEARNING_HUELLE_FLAECHE_BASIS": "https://lernen.example.test:8443",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
    "LEARNING_HUELLE_ACTOR": "lh-1-inhaber",
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": KURS, "titel": "Notwegkurs"}]),
}
E = einstellung_lesen({**BASIS, "LEARNING_HUELLE_LEARNHOUSE_URL": f" {LH}/ "})


class Uhr:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _katalog(antwort, einstellung=E) -> tuple[Katalog, list[httpx.Request], Uhr]:
    gesehen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return antwort(request) if callable(antwort) else antwort

    uhr = Uhr()
    return Katalog(einstellung, httpx.Client(transport=httpx.MockTransport(handler)), uhr=uhr), gesehen, uhr


def _kurse(*eintraege: dict) -> httpx.Response:
    return httpx.Response(200, json={"kurse": list(eintraege)})


def test_texte_woertlich() -> None:
    assert KEIN_ZUGANG_MEHR == "Ihr Zugang zu diesem Kurs besteht nicht mehr. Öffnen Sie die Kursübersicht."
    assert UEBERSICHT_WEG == "Die Kursübersicht ist gerade nicht erreichbar. Bitte versuchen Sie es gleich noch einmal."
    assert PUFFER_S == 60 and TIMEOUT_S == 5


def test_einstellung_learnhouse_url_optional() -> None:
    assert einstellung_lesen(BASIS).learnhouse_url == ""
    assert E.learnhouse_url == LH
    with pytest.raises(RuntimeError, match="LEARNING_HUELLE_LEARNHOUSE_URL"):
        einstellung_lesen({**BASIS, "LEARNING_HUELLE_LEARNHOUSE_URL": "ftp://x.test"})


def test_notweg_kennung_bekommt_die_kurse_der_umgebung() -> None:
    katalog, gesehen, _ = _katalog(_kurse())
    assert katalog.kurse("lh-1-inhaber") == (Kurs(KURS, "Notwegkurs"),)
    assert gesehen == []


def test_ohne_learnhouse_nur_der_notweg() -> None:
    katalog, gesehen, _ = _katalog(_kurse(), einstellung_lesen(BASIS))
    assert katalog.kurse("lh-1-inhaber") == (Kurs(KURS, "Notwegkurs"),)
    with pytest.raises(KeinZugang):
        katalog.kurse("person-a")
    assert gesehen == []


def test_person_fragt_die_dienstroute_mit_schluessel_und_timeout() -> None:
    katalog, gesehen, _ = _katalog(_kurse({"course_id": KURS_B.upper(), "titel": "Kurs B"}))
    assert katalog.kurse("lh-1-person-a") == (Kurs(KURS_B, "Kurs B"),)
    anfrage = gesehen[0]
    assert anfrage.method == "GET"
    assert str(anfrage.url) == f"{LH}/api/v1/learning/personen/lh-1-person-a/kurse"
    assert anfrage.headers["X-LearnHouse-Learning-Service-Key"] == SCHLUESSEL
    assert anfrage.extensions["timeout"] == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}


def test_puffer_60_s_je_kennung() -> None:
    katalog, gesehen, uhr = _katalog(lambda r: _kurse({"course_id": KURS_B, "titel": r.url.path.split("/")[-2]}))
    katalog.kurse("person-a")
    uhr.t += 59.9
    assert katalog.kurse("person-a") == (Kurs(KURS_B, "person-a"),)
    assert len(gesehen) == 1
    assert katalog.kurse("person-b") == (Kurs(KURS_B, "person-b"),)  # eigener Eintrag je Kennung
    assert len(gesehen) == 2
    uhr.t += 0.2
    katalog.kurse("person-a")
    assert len(gesehen) == 3


def test_entfernt_404_kein_zugang_ohne_puffer() -> None:
    antworten = [_kurse({"course_id": KURS_B, "titel": "B"}), httpx.Response(404), httpx.Response(404)]
    katalog, gesehen, uhr = _katalog(lambda r: antworten.pop(0))
    assert katalog.kurse("person-a")
    uhr.t += PUFFER_S
    with pytest.raises(KeinZugang):
        katalog.kurse("person-a")
    with pytest.raises(KeinZugang):  # 404 wird nicht gepuffert
        katalog.kurse("person-a")
    assert len(gesehen) == 3


@pytest.mark.parametrize("antwort", [
    httpx.Response(500), httpx.Response(503), httpx.Response(403), httpx.Response(200, text="kein json"),
    httpx.Response(200, json=[]), httpx.Response(200, json={"kurse": "x"}), httpx.Response(302),
])
def test_uebersicht_weg_bei_5xx_und_kaputter_antwort(antwort: httpx.Response) -> None:
    katalog, _, _ = _katalog(antwort)
    with pytest.raises(UebersichtWeg):
        katalog.kurse("person-a")


def test_uebersicht_weg_bei_netzfehler_ohne_werte_im_log(caplog: pytest.LogCaptureFixture) -> None:
    # Eigene Meldungen bis DEBUG; httpx selbst nennt die URL nur auf INFO (die Huelle loggt ab WARNING).
    caplog.set_level(logging.DEBUG, logger="spaces.learning.huelle.katalog")

    def weg(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("weg")

    katalog, _, _ = _katalog(weg)
    with pytest.raises(UebersichtWeg):
        katalog.kurse("lh-1-person-geheim")
    assert "ConnectTimeout" in caplog.text
    assert "person-geheim" not in caplog.text and SCHLUESSEL not in caplog.text and "learnhouse.test" not in caplog.text


def test_eintraege_ohne_uuid_fallen_weg(caplog: pytest.LogCaptureFixture) -> None:
    # Eigene Meldungen bis DEBUG; httpx selbst nennt die URL nur auf INFO (die Huelle loggt ab WARNING).
    caplog.set_level(logging.DEBUG, logger="spaces.learning.huelle.katalog")
    katalog, _, _ = _katalog(_kurse(
        {"course_id": "kein-uuid-geheim", "titel": "X"},
        {"course_id": KURS_B, "titel": "B"},
        {"titel": "ohne"},
        "kaputt",
        {"course_id": KURS, "titel": 7},
    ))
    assert katalog.kurse("person-a") == (Kurs(KURS_B, "B"),)
    assert "kein-uuid-geheim" not in caplog.text and "person-a" not in caplog.text


def test_kennung_wird_in_der_url_kodiert() -> None:
    katalog, gesehen, _ = _katalog(_kurse())
    assert katalog.kurse("lh:1.person") == ()
    assert gesehen[0].url.raw_path == b"/api/v1/learning/personen/lh%3A1.person/kurse"
