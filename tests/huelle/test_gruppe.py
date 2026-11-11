"""Teil C: Gruppen-Zugang (LearnHouse), Ausweis und Dienstaufrufe an den Abgleichdienst."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.gruppe import (
    GRUPPE_VOLL,
    GRUPPE_WEG,
    KEIN_MITGLIED,
    ZUGANG_WEG,
    AbgleichDienst,
    Gruppen,
    GruppeWeg,
    KeinMitglied,
    Mitgliedschaft,
    ausweis,
    flaeche_der_gruppe,
    gruppe_aus_flaeche,
    person_kennung,
)
from spaces.learning.huelle.katalog import UebersichtWeg

VEKTOR = Path(__file__).with_name("ausweis_vektor.json")
SERVICE_KEY = "s" * 40
LH = "http://learnhouse.test"
DIENST = "http://abgleich.test:8160"
ABGLEICH_SCHLUESSEL = "a" * 40
KURS = "55555555-5555-4555-8555-555555555555"
ACTOR_A = "lh-1-person-a"


# ---- Ausweis und Kennungen ------------------------------------------------------------------------------------

def test_ausweis_vektor_gleich_node() -> None:
    v = json.loads(VEKTOR.read_bytes())
    assert ausweis(v["schluessel"], v["flaeche"], v["person"], v["name"], v["ablauf"]) == v["ausweis"]


def test_ausweis_form_wie_node() -> None:
    wert = ausweis(ABGLEICH_SCHLUESSEL, "g-7", "p" + "0" * 24, "", 2_000_000_000)
    teil, sig = wert.split(".")
    nutzlast = base64.urlsafe_b64decode(teil + "=" * (-len(teil) % 4)).decode()
    assert nutzlast == f"g-7|p{'0' * 24}|2000000000|"
    assert len(sig) == 48 and all(z in "0123456789abcdef" for z in sig)
    assert "=" not in teil


def test_person_kennung_form() -> None:
    kennung = person_kennung(ACTOR_A)
    assert kennung == "p" + hashlib.sha256(ACTOR_A.encode()).hexdigest()[:24]
    assert len(kennung) == 25 and kennung[0] == "p" and all(z in "0123456789abcdef" for z in kennung[1:])
    assert person_kennung("lh-1-person-b") != kennung


def test_flaeche_der_gruppe_hin_und_zurueck() -> None:
    assert flaeche_der_gruppe(7) == "g-7"
    assert gruppe_aus_flaeche("g-7") == 7
    assert gruppe_aus_flaeche("g-123456789") == 123456789
    for falsch in ("g-", "g-1234567890", "g-07", "G-7", "g-7\n", " g-7", "lf-x", "g-7&x", "g--7", "", "g-٣"):
        assert gruppe_aus_flaeche(falsch) is None, falsch


def test_texte_woertlich() -> None:
    assert KEIN_MITGLIED == "Sie gehören nicht zu dieser Gruppe. Öffnen Sie die Kursübersicht."
    assert GRUPPE_WEG == "Die Gruppenfläche ist gerade nicht erreichbar. Versuchen Sie es in einer Minute noch einmal."
    assert GRUPPE_VOLL == "Diese Gruppenfläche ist voll. Bitte wenden Sie sich an Ihre Kursleitung."
    assert ZUGANG_WEG == "Ihr Zugang zu dieser Gruppe besteht nicht mehr."
    # Websocket-Schliessgruende duerfen hoechstens 123 Bytes haben.
    for text in (KEIN_MITGLIED, GRUPPE_WEG, ZUGANG_WEG):
        assert len(text.encode("utf-8")) <= 123


# ---- Gruppen (LearnHouse-Dienstroute) ------------------------------------------------------------------------

class FakeLearnHouse:
    """`GET /api/v1/learning/gruppen/{gid}/personen/{actor}`: Antworten je (gid, actor), sonst 404."""

    def __init__(self) -> None:
        self.anfragen: list[httpx.Request] = []
        self.antworten: dict[tuple[str, str], Any] = {}
        self.fehler: Exception | None = None
        self.status = 200

    def eintragen(self, gid: int, actor: str, **felder: Any) -> None:
        daten = {"mitglied": True, "offen": True, "course_id": KURS, "gruppe_name": "Montag",
                 "anzeigename": "Anna A."}
        daten.update(felder)
        self.antworten[(str(gid), actor)] = daten

    def __call__(self, anfrage: httpx.Request) -> httpx.Response:
        self.anfragen.append(anfrage)
        if self.fehler is not None:
            raise self.fehler
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "x"})
        teile = anfrage.url.raw_path.decode().split("/")
        # /api/v1/learning/gruppen/{gid}/personen/{actor}
        gid, actor = teile[5], unquote(teile[7])
        daten = self.antworten.get((gid, actor))
        if daten is None:
            return httpx.Response(404, json={"detail": "Nicht gefunden"})
        return httpx.Response(200, json=daten)


class Uhr:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _gruppen(lh: FakeLearnHouse, uhr: Uhr | None = None) -> Gruppen:
    return Gruppen(LH + "/", SERVICE_KEY, http=httpx.Client(transport=httpx.MockTransport(lh)), uhr=uhr or Uhr())


def test_mitglied_url_und_dienstkopf() -> None:
    lh = FakeLearnHouse()
    lh.eintragen(7, "lh-1-a/b")
    m = _gruppen(lh).mitglied("lh-1-a/b", 7)
    assert m == Mitgliedschaft(course_id=KURS, gruppe_name="Montag", anzeigename="Anna A.", offen=True)
    anfrage = lh.anfragen[0]
    assert anfrage.method == "GET"
    assert anfrage.url.raw_path == b"/api/v1/learning/gruppen/7/personen/lh-1-a%2Fb"
    assert anfrage.url.host == "learnhouse.test"
    assert anfrage.headers["X-LearnHouse-Learning-Service-Key"] == SERVICE_KEY


def test_mitglied_puffer_60s() -> None:
    lh, uhr = FakeLearnHouse(), Uhr()
    lh.eintragen(7, ACTOR_A)
    gruppen = _gruppen(lh, uhr)
    assert gruppen.mitglied(ACTOR_A, 7).gruppe_name == "Montag"
    uhr.t += 59
    lh.eintragen(7, ACTOR_A, gruppe_name="Dienstag")
    assert gruppen.mitglied(ACTOR_A, 7).gruppe_name == "Montag"  # aus dem Puffer
    assert len(lh.anfragen) == 1
    uhr.t += 1
    assert gruppen.mitglied(ACTOR_A, 7).gruppe_name == "Dienstag"  # nach 60 s frisch
    assert len(lh.anfragen) == 2
    # Andere Gruppe oder andere Person: eigener Eintrag
    lh.eintragen(8, ACTOR_A)
    gruppen.mitglied(ACTOR_A, 8)
    assert len(lh.anfragen) == 3


def test_mitglied_frisch_am_puffer_vorbei() -> None:
    lh, uhr = FakeLearnHouse(), Uhr()
    lh.eintragen(7, ACTOR_A)
    gruppen = _gruppen(lh, uhr)
    gruppen.mitglied(ACTOR_A, 7)
    del lh.antworten[("7", ACTOR_A)]
    with pytest.raises(KeinMitglied):
        gruppen.mitglied(ACTOR_A, 7, frisch=True)
    assert len(lh.anfragen) == 2
    # Die Entfernung loescht auch den Puffer: der naechste normale Aufruf fragt wieder.
    with pytest.raises(KeinMitglied):
        gruppen.mitglied(ACTOR_A, 7)
    assert gruppen.gemerkt(ACTOR_A, 7) is None


def test_kein_mitglied_404() -> None:
    lh = FakeLearnHouse()
    with pytest.raises(KeinMitglied):
        _gruppen(lh).mitglied(ACTOR_A, 7)


def test_kein_mitglied_bei_mitglied_false() -> None:
    lh = FakeLearnHouse()
    lh.eintragen(7, ACTOR_A, mitglied=False)
    gruppen = _gruppen(lh)
    with pytest.raises(KeinMitglied):
        gruppen.mitglied(ACTOR_A, 7)
    with pytest.raises(KeinMitglied):  # nicht gepuffert: eine neu aufgenommene Person kommt sofort hinein
        gruppen.mitglied(ACTOR_A, 7)
    assert len(lh.anfragen) == 2


def test_geschlossene_gruppe_bleibt_mitgliedschaft_und_name_leer() -> None:
    lh = FakeLearnHouse()
    lh.eintragen(7, ACTOR_A, offen=False, anzeigename=None)
    assert _gruppen(lh).mitglied(ACTOR_A, 7) == Mitgliedschaft(KURS, "Montag", "", False)


@pytest.mark.parametrize("fehler", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
def test_netzfehler_uebersicht_weg(fehler: Exception) -> None:
    lh = FakeLearnHouse()
    lh.fehler = fehler
    with pytest.raises(UebersichtWeg):
        _gruppen(lh).mitglied(ACTOR_A, 7)


@pytest.mark.parametrize("status", [500, 502, 503, 403, 401, 400])
def test_status_fehler_uebersicht_weg(status: int) -> None:
    # 5xx: LearnHouse weg; 401/403/503: Dienstschluessel stimmt nicht. Beides ist kein "kein Mitglied".
    lh = FakeLearnHouse()
    lh.status = status
    with pytest.raises(UebersichtWeg):
        _gruppen(lh).mitglied(ACTOR_A, 7)


@pytest.mark.parametrize("daten", [
    {"mitglied": True}, {"mitglied": True, "offen": True, "course_id": "kein-uuid", "gruppe_name": "M",
                         "anzeigename": None},
    {"mitglied": "ja", "offen": True, "course_id": KURS, "gruppe_name": "M", "anzeigename": None},
    {"mitglied": True, "offen": True, "course_id": KURS, "gruppe_name": 5, "anzeigename": None},
    {"mitglied": True, "offen": "ja", "course_id": KURS, "gruppe_name": "M", "anzeigename": None},
    {"mitglied": True, "offen": True, "course_id": KURS, "gruppe_name": "M", "anzeigename": 3},
    [1, 2],
])
def test_kaputte_antwort_uebersicht_weg(daten: Any) -> None:
    def antwort(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=daten)

    gruppen = Gruppen(LH, SERVICE_KEY, http=httpx.Client(transport=httpx.MockTransport(antwort)), uhr=Uhr())
    with pytest.raises(UebersichtWeg):
        gruppen.mitglied(ACTOR_A, 7)


def test_gemerkt_ueberlebt_den_ausfall() -> None:
    lh, uhr = FakeLearnHouse(), Uhr()
    lh.eintragen(7, ACTOR_A)
    gruppen = _gruppen(lh, uhr)
    assert gruppen.gemerkt(ACTOR_A, 7) is None
    gruppen.mitglied(ACTOR_A, 7)
    uhr.t += 3600
    lh.fehler = httpx.ConnectError("x")
    with pytest.raises(UebersichtWeg):
        gruppen.mitglied(ACTOR_A, 7)
    assert gruppen.gemerkt(ACTOR_A, 7) == Mitgliedschaft(KURS, "Montag", "Anna A.", True)
    assert gruppen.gemerkt(ACTOR_A, 8) is None


@pytest.mark.parametrize("gid", [0, -1, 1_000_000_000, True])
def test_ungueltige_gruppe_ohne_anfrage(gid: Any) -> None:
    lh = FakeLearnHouse()
    with pytest.raises(KeinMitglied):
        _gruppen(lh).mitglied(ACTOR_A, gid)
    assert lh.anfragen == []


def test_log_ohne_kennung_und_schluessel(caplog: pytest.LogCaptureFixture) -> None:
    lh = FakeLearnHouse()
    lh.fehler = httpx.ConnectError(f"{LH} {ACTOR_A}")
    with caplog.at_level(logging.DEBUG, logger="spaces.learning.huelle"), pytest.raises(UebersichtWeg):
        _gruppen(lh).mitglied(ACTOR_A, 7)
    lh.fehler, lh.status = None, 503
    with caplog.at_level(logging.DEBUG, logger="spaces.learning.huelle"), pytest.raises(UebersichtWeg):
        _gruppen(lh).mitglied(ACTOR_A, 7)
    text = caplog.text
    assert caplog.records
    for geheim in (ACTOR_A, SERVICE_KEY, "learnhouse.test"):
        assert geheim not in text


# ---- AbgleichDienst (/platz, /schutz) ------------------------------------------------------------------------

class FakeDienst:
    def __init__(self) -> None:
        self.anfragen: list[httpx.Request] = []
        self.fehler: Exception | None = None
        self.status: int | None = None
        self.platz: Any = 3

    def __call__(self, anfrage: httpx.Request) -> httpx.Response:
        self.anfragen.append(anfrage)
        if self.fehler is not None:
            raise self.fehler
        if self.status is not None:
            return httpx.Response(self.status, json={"fehler": "x"})
        if anfrage.url.path == "/platz":
            return httpx.Response(200, json={"platz": self.platz})
        return httpx.Response(204)


def _dienst(fake: FakeDienst) -> AbgleichDienst:
    return AbgleichDienst(DIENST + "/", ABGLEICH_SCHLUESSEL, http=httpx.Client(transport=httpx.MockTransport(fake)))


def test_dienstkopf_wie_node() -> None:
    fake = FakeDienst()
    _dienst(fake).platz("g-7", "p" + "1" * 24)
    erwartet = hashlib.sha256(f"abgleich-dienst:{ABGLEICH_SCHLUESSEL}".encode()).hexdigest()
    assert fake.anfragen[0].headers["X-Abgleich-Dienst"] == erwartet


def test_platz_post_und_antwort() -> None:
    fake = FakeDienst()
    person = "p" + "1" * 24
    assert _dienst(fake).platz("g-7", person) == 3
    anfrage = fake.anfragen[0]
    assert anfrage.method == "POST" and str(anfrage.url) == f"{DIENST}/platz"
    assert json.loads(anfrage.content) == {"flaeche": "g-7", "person": person}


def test_schutz_put() -> None:
    fake = FakeDienst()
    person = "p" + "1" * 24
    assert _dienst(fake).schutz_setzen("g-7", person, [[0, 0, 100, 50], [200, 300, 10, 10]]) is None
    anfrage = fake.anfragen[0]
    assert anfrage.method == "PUT" and str(anfrage.url) == f"{DIENST}/schutz"
    assert json.loads(anfrage.content) == {"flaeche": "g-7", "person": person,
                                           "felder": [[0, 0, 100, 50], [200, 300, 10, 10]]}


@pytest.mark.parametrize("art", ["netz", "timeout", 500, 503, 403, 400])
def test_dienst_weg(art: Any) -> None:
    fake = FakeDienst()
    if art == "netz":
        fake.fehler = httpx.ConnectError("x")
    elif art == "timeout":
        fake.fehler = httpx.ReadTimeout("x")
    else:
        fake.status = art
    dienst = _dienst(fake)
    with pytest.raises(GruppeWeg):
        dienst.platz("g-7", "p" + "1" * 24)
    with pytest.raises(GruppeWeg):
        dienst.schutz_setzen("g-7", "p" + "1" * 24, [])


@pytest.mark.parametrize("platz", [None, "3", -1, 1.5, True])
def test_platz_kaputt_gruppe_weg(platz: Any) -> None:
    fake = FakeDienst()
    fake.platz = platz
    with pytest.raises(GruppeWeg):
        _dienst(fake).platz("g-7", "p" + "1" * 24)


# ---- Einstellung -----------------------------------------------------------------------------------------------

ENV = {
    "LEARNING_HUELLE_BASIS": "https://lernen.example.test",
    "LEARNING_HUELLE_FLAECHE_BASIS": "https://lernen.example.test:8443",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SERVICE_KEY,
    "LEARNING_HUELLE_ACTOR": "lh-1-test",
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": KURS, "titel": "T"}]),
}


def test_einstellung_gruppen_nur_mit_allen_dreien() -> None:
    voll = {**ENV, "LEARNING_HUELLE_LEARNHOUSE_URL": LH, "LEARNING_HUELLE_ABGLEICH_URL": DIENST,
            "LEARNING_HUELLE_ABGLEICH_SCHLUESSEL": ABGLEICH_SCHLUESSEL}
    e = einstellung_lesen(voll)
    assert e.gruppen_aktiv and e.abgleich_url == DIENST and e.abgleich_schluessel == ABGLEICH_SCHLUESSEL
    for fehlt in ("LEARNING_HUELLE_LEARNHOUSE_URL", "LEARNING_HUELLE_ABGLEICH_URL",
                  "LEARNING_HUELLE_ABGLEICH_SCHLUESSEL"):
        assert not einstellung_lesen({k: v for k, v in voll.items() if k != fehlt}).gruppen_aktiv, fehlt
    assert not einstellung_lesen(ENV).gruppen_aktiv


def test_einstellung_crlf_wird_entfernt() -> None:
    e = einstellung_lesen({**ENV, "LEARNING_HUELLE_LEARNHOUSE_URL": LH + "\r",
                           "LEARNING_HUELLE_ABGLEICH_URL": f" {DIENST}/\r\n",
                           "LEARNING_HUELLE_ABGLEICH_SCHLUESSEL": f"{ABGLEICH_SCHLUESSEL}\r\n"})
    assert e.abgleich_url == DIENST and e.abgleich_schluessel == ABGLEICH_SCHLUESSEL and e.gruppen_aktiv


def test_einstellung_schluessel_zu_kurz_und_url_falsch() -> None:
    with pytest.raises(RuntimeError, match="LEARNING_HUELLE_ABGLEICH_SCHLUESSEL ist zu kurz") as fehler:
        # 31 Zeichen plus CRLF: der Rand zaehlt nicht mit
        einstellung_lesen({**ENV, "LEARNING_HUELLE_ABGLEICH_SCHLUESSEL": "x" * 31 + "\r\n"})
    assert "x" * 31 not in str(fehler.value)
    for falsch in ("ftp://abgleich.test", "http://", "http://u:p@abgleich.test", "http://abgleich.test/?x=1"):
        with pytest.raises(RuntimeError, match="LEARNING_HUELLE_ABGLEICH_URL"):
            einstellung_lesen({**ENV, "LEARNING_HUELLE_ABGLEICH_URL": falsch})


def test_einstellung_schluessel_nicht_im_repr() -> None:
    e = einstellung_lesen({**ENV, "LEARNING_HUELLE_ABGLEICH_SCHLUESSEL": ABGLEICH_SCHLUESSEL})
    assert ABGLEICH_SCHLUESSEL not in repr(e)
