"""Abgabe einer Programmieraufgabe: die Huelle laesst die Testfaelle im Code-Laeufer laufen und schickt
das Ergebnis mit. Der Laeufer ist hier ein Fake; es laeuft kein Code."""

from __future__ import annotations

from typing import Any

from spaces.learning.huelle.laeufer import Lauf, LaeuferNichtBereit
from spaces.learning.huelle.lern_mcp import LearningAbgelehnt

from .test_bruecke import SID, TASK_1, TASK_2, FakeLern, _offen, _ueberblick, turn

PFAD = f"/lernen/api/sitzungen/{SID}/antwort"
CODE = "import sys\nprint(sum(map(int, sys.stdin.read().split())))"
TESTS = [{"name": "drei Zahlen", "eingabe": "3 4 5", "erwartet": "12"},
         {"name": "leer", "eingabe": "", "erwartet": "0"}]
DATEN = {"attempt_id": "a1", "item_type": "code", "art": "code", "aufgabe": {}, "sprache": "python", "tests": TESTS}
GUT = Lauf(0, "12\n", "", False, False, 3)
NULL = Lauf(0, "0\n", "", False, False, 3)
FALSCH = Lauf(0, "7\n", "", False, False, 3)


class FolgeLaeufer:
    def __init__(self, *laeufe: Lauf | Exception) -> None:
        self.laeufe = list(laeufe)
        self.aufrufe: list[tuple[Any, ...]] = []

    def __call__(self, sprache: Any, quelltext: Any, eingabe: Any = "") -> Lauf:
        self.aufrufe.append((sprache, quelltext, eingabe))
        lauf = self.laeufe.pop(0)
        if isinstance(lauf, Exception):
            raise lauf
        return lauf


def _aufbau(*laeufe: Lauf | Exception, daten: Any = DATEN, typ: str = "code"):
    offen = turn(TASK_1, typ=typ, mode="training")
    offen["task"]["presentation"] = {"sprache": "python", "startcode": ""}
    naechster = turn(TASK_2, completed=1, revision=2, mode="training", feedback={
        "state": "partial", "score": 0.69, "rationale_codes": [], "source_refs": [],
        "criteria": [{"criterion_id": "c1", "description": "Lesbarer Code", "score": 0.5, "feedback": "Namen unklar."}]})
    lern = FakeLern({
        "learning_course_overview": _ueberblick(offen),
        "learning_task_next": [{"session": offen}],
        "learning_task_answer": {"session": naechster},
        "learning_task_bewertungsdaten": daten,
    })
    client, _, penecho, _ = _offen(lern)
    laeufer = FolgeLaeufer(*laeufe)
    client.app._laeufer = laeufer
    return lern, client, penecho, laeufer


def _abgabe(lern: FakeLern) -> dict[str, Any]:
    return [kw for tool, kw in lern.aufrufe if tool == "learning_task_answer"][-1]["payload"]


def test_alle_tests_bestanden_gehen_als_eigenes_feld_an_learning() -> None:
    lern, client, penecho, laeufer = _aufbau(GUT, NULL)
    r = client.post(PFAD, json={"revision": 1, "answer": {"text": CODE}})
    assert r.status_code == 200, r.text
    assert laeufer.aufrufe == [("python", CODE, "3 4 5"), ("python", CODE, "")]
    assert _abgabe(lern) == {"answer": {"text": CODE},
                             "testlauf": {"bestanden": 2, "gesamt": 2, "fehlgeschlagen": []}}
    daten_kw = [kw for tool, kw in lern.aufrufe if tool == "learning_task_bewertungsdaten"][-1]
    assert daten_kw["expected_revision"] == 1 and not daten_kw.get("schreibend")
    assert r.json()["rueckmeldung"].startswith("Alle 2 Tests bestanden.")


def test_gescheiterte_tests_stehen_vorn_in_der_rueckmeldung() -> None:
    lern, client, penecho, _ = _aufbau(GUT, FALSCH)
    r = client.post(PFAD, json={"revision": 1, "answer": {"text": CODE}})
    assert r.status_code == 200, r.text
    assert _abgabe(lern)["testlauf"] == {"bestanden": 1, "gesamt": 2, "fehlgeschlagen": ["leer"]}
    text = r.json()["rueckmeldung"]
    assert text.startswith("1 von 2 Tests bestanden. Es fehlt: leer.")
    assert "Namen unklar." in text and text.index("Es fehlt: leer") < text.index("Namen unklar.")
    # Dieselbe Rueckmeldung steht auf der Karte der Flaeche.
    karte = [a for a in penecho.aufrufe if a[0] == "rueckmeldung_setzen"][-1]
    assert karte[-1] == text


def test_laeufer_nicht_erreichbar_gibt_nicht_ab() -> None:
    lern, client, _, _ = _aufbau(LaeuferNichtBereit("exit 1"))
    r = client.post(PFAD, json={"revision": 1, "answer": {"text": CODE}})
    assert r.status_code == 503 and r.json()["code"] == "laeufer_weg"
    assert "learning_task_answer" not in lern.tools()
    assert "exit 1" not in r.text


def test_aufgabe_ohne_tests_wird_wie_bisher_abgegeben() -> None:
    lern, client, _, laeufer = _aufbau(daten={**DATEN, "tests": []})
    r = client.post(PFAD, json={"revision": 1, "answer": {"text": CODE}})
    assert r.status_code == 200, r.text
    assert _abgabe(lern) == {"answer": {"text": CODE}} and laeufer.aufrufe == []
    assert "Tests" not in r.json()["rueckmeldung"]


def test_learning_ohne_testfaelle_fuer_code_stoert_die_abgabe_nicht() -> None:
    # Aeltere Learning-Fassung oder Pruefung: keine Bewertungsdaten fuer code.
    lern, client, _, laeufer = _aufbau(daten=LearningAbgelehnt("no grading data", "invalid_request"))
    r = client.post(PFAD, json={"revision": 1, "answer": {"text": CODE}})
    assert r.status_code == 200, r.text
    assert _abgabe(lern) == {"answer": {"text": CODE}} and laeufer.aufrufe == []


def test_ein_testlauf_aus_dem_browser_wird_verworfen() -> None:
    lern, client, _, _ = _aufbau(GUT, FALSCH)
    gefaelscht = {"text": CODE, "testlauf": {"bestanden": 2, "gesamt": 2, "fehlgeschlagen": []}}
    r = client.post(PFAD, json={"revision": 1, "answer": gefaelscht})
    assert r.status_code == 200, r.text
    assert _abgabe(lern) == {"answer": {"text": CODE},
                             "testlauf": {"bestanden": 1, "gesamt": 2, "fehlgeschlagen": ["leer"]}}


def test_andere_aufgabentypen_fragen_keine_testfaelle_ab() -> None:
    lern, client, _, laeufer = _aufbau(typ="open")
    r = client.post(PFAD, json={"revision": 1, "answer": {"text": "frei"}})
    assert r.status_code == 200, r.text
    assert "learning_task_bewertungsdaten" not in lern.tools() and laeufer.aufrufe == []


def test_listenkurs_laesst_die_tests_laufen_und_schickt_sie_mit_dem_versuch() -> None:
    from .test_bruecke import ListenLern, _aufrufe, _listen_offen

    ll = ListenLern(3)
    ll.eintraege[1]["task"]["type"] = "code"
    ll.lern.antworten["learning_task_bewertungsdaten"] = DATEN
    client, _, _ = _listen_offen(ll)
    laeufer = FolgeLaeufer(GUT, FALSCH)
    client.app._laeufer = laeufer
    try:
        r = client.post(PFAD, json={"nummer": 2, "answer": {"text": CODE, "testlauf": {"bestanden": 9}}})
        assert r.status_code == 200, r.text
        (kw,) = _aufrufe(ll.lern, "learning_task_answer")
        assert kw["payload"] == {
            "answer": {"text": CODE}, "attempt_id": ll.eintraege[1]["attempt_id"],
            "testlauf": {"bestanden": 1, "gesamt": 2, "fehlgeschlagen": ["leer"]},
        }
        (daten_kw,) = _aufrufe(ll.lern, "learning_task_bewertungsdaten")
        assert daten_kw["payload"] == {"attempt_id": ll.eintraege[1]["attempt_id"]}
        assert laeufer.aufrufe == [("python", CODE, "3 4 5"), ("python", CODE, "")]
        assert r.json()["rueckmeldung"].startswith("1 von 2 Tests bestanden. Es fehlt: leer.")
    finally:
        client.app.beenden()
