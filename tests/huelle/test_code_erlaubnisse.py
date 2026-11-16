"""Erlaubnisse beim Ausfuehren und Abgeben: erlaubt ist, was die Person will UND die Aufgabe hoechstens zulaesst.
Der Laeufer ist ein Fake; es laeuft kein Code."""

from __future__ import annotations

import base64
from typing import Any

from spaces.learning.huelle.laeufer import Bild, Lauf
from spaces.learning.huelle.lern_mcp import LearningAbgelehnt

from .test_bruecke import SID, TASK_1, TASK_2, FakeLern, _offen, _ueberblick, turn

AUSFUEHREN = f"/lernen/api/sitzungen/{SID}/ausfuehren"
ANTWORT = f"/lernen/api/sitzungen/{SID}/antwort"
PNG = b"\x89PNG\r\n\x1a\n" + b"bild"
CODE = "import numpy as np\nprint(1)"
TESTS = [{"name": "eins", "eingabe": "", "erwartet": "1"}]
DATEN = {"attempt_id": "a1", "item_type": "code", "art": "code", "aufgabe": {}, "sprache": "python", "tests": TESTS,
         "erlaubnisse": ["pakete", "diagramm", "daten"], "dateien": [{"name": "umsatz.csv", "inhalt": "1;2\n"}]}


class MerkLaeufer:
    """Merkt sich jeden Aufruf samt Erlaubnissen; liefert immer denselben Lauf."""

    def __init__(self, lauf: Lauf) -> None:
        self.lauf = lauf
        self.aufrufe: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, sprache: Any, quelltext: Any, eingabe: Any = "", **kw: Any) -> Lauf:
        self.aufrufe.append(((sprache, quelltext, eingabe), kw))
        return self.lauf


def _aufbau(daten: Any = DATEN, lauf: Lauf | None = None):
    offen = turn(TASK_1, typ="code", mode="training")
    offen["task"]["presentation"] = {"sprache": "python", "startcode": "", "erlaubnisse": ["pakete", "diagramm", "daten"]}
    naechster = turn(TASK_2, completed=1, revision=2, mode="training",
                     feedback={"state": "correct", "score": 1.0, "rationale_codes": [], "source_refs": [], "criteria": []})
    lern = FakeLern({
        "learning_course_overview": _ueberblick(offen),
        "learning_task_next": [{"session": offen}],
        "learning_task_answer": {"session": naechster},
        "learning_task_bewertungsdaten": daten,
    })
    client, _, _, _ = _offen(lern)
    laeufer = MerkLaeufer(lauf or Lauf(0, "1\n", "", False, False, 3))
    client.app._laeufer = laeufer
    return lern, client, laeufer


def test_ohne_zustimmung_laeuft_die_grundstufe_und_learning_wird_nicht_gefragt() -> None:
    lern, client, laeufer = _aufbau()
    r = client.post(AUSFUEHREN, json={"sprache": "python", "quelltext": CODE})
    assert r.status_code == 200, r.text
    assert laeufer.aufrufe == [(("python", CODE, ""), {})]
    assert "learning_task_bewertungsdaten" not in lern.tools()
    assert r.json()["erlaubt"] == [] and r.json()["bilder"] == []


def test_zustimmung_gilt_nur_innerhalb_der_obergrenze_der_aufgabe() -> None:
    lern, client, laeufer = _aufbau(lauf=Lauf(0, "1\n", "", False, False, 3, (Bild("kurve.png", PNG),)))
    # Die Person stimmt Paketen, Diagramm und langer Laufzeit zu; die Aufgabe erlaubt keine lange Laufzeit.
    r = client.post(AUSFUEHREN, json={"sprache": "python", "quelltext": CODE,
                                      "erlaubt": ["pakete", "diagramm", "lange_laufzeit", "netz", 7]})
    assert r.status_code == 200, r.text
    (_, kw), = laeufer.aufrufe
    assert kw == {"erlaubnisse": ("pakete", "diagramm"), "dateien": (), "zeit_s": 5}
    body = r.json()
    assert body["erlaubt"] == ["pakete", "diagramm"]
    assert body["bilder"] == [{"name": "kurve.png", "png_base64": base64.b64encode(PNG).decode()}]


def test_uebungsdaten_kommen_von_learning_nie_aus_dem_browser() -> None:
    lern, client, laeufer = _aufbau()
    r = client.post(AUSFUEHREN, json={"sprache": "python", "quelltext": CODE, "erlaubt": ["daten"],
                                      "dateien": [{"name": "boese.csv", "inhalt": "x"}]})
    assert r.status_code == 200, r.text
    (_, kw), = laeufer.aufrufe
    assert kw == {"erlaubnisse": ("daten",), "dateien": (("umsatz.csv", "1;2\n"),), "zeit_s": 5}


def test_lange_laufzeit_gibt_zwanzig_sekunden() -> None:
    lern, client, laeufer = _aufbau(daten={**DATEN, "erlaubnisse": ["lange_laufzeit"], "dateien": []})
    client.post(AUSFUEHREN, json={"sprache": "python", "quelltext": CODE, "erlaubt": ["lange_laufzeit"]})
    assert laeufer.aufrufe[0][1] == {"erlaubnisse": ("lange_laufzeit",), "dateien": (), "zeit_s": 20}


def test_ohne_obergrenze_von_learning_bleibt_es_bei_der_grundstufe() -> None:
    # Aeltere Learning-Fassung, Pruefung oder Aufgabe ohne Erlaubnisse: die Zustimmung allein oeffnet nichts.
    for daten in (LearningAbgelehnt("no grading data", "invalid_request"), {**DATEN, "erlaubnisse": [], "dateien": []}):
        lern, client, laeufer = _aufbau(daten=daten)
        r = client.post(AUSFUEHREN, json={"sprache": "python", "quelltext": CODE, "erlaubt": ["pakete"]})
        assert r.status_code == 200, r.text
        assert laeufer.aufrufe == [(("python", CODE, ""), {})] and r.json()["erlaubt"] == []


def test_abgabe_laesst_die_tests_mit_der_zustimmung_der_person_laufen() -> None:
    lern, client, laeufer = _aufbau()
    r = client.post(ANTWORT, json={"revision": 1, "answer": {"text": CODE}, "erlaubt": ["pakete", "daten", "lange_laufzeit"]})
    assert r.status_code == 200, r.text
    (aufruf, kw), = laeufer.aufrufe
    assert aufruf == ("python", CODE, "")
    assert kw == {"erlaubnisse": ("pakete", "daten"), "dateien": (("umsatz.csv", "1;2\n"),), "zeit_s": 5}
    abgabe = [k for tool, k in lern.aufrufe if tool == "learning_task_answer"][-1]["payload"]
    assert abgabe == {"answer": {"text": CODE}, "testlauf": {"bestanden": 1, "gesamt": 1, "fehlgeschlagen": []}}


def test_abgabe_ohne_zustimmung_laeuft_in_der_grundstufe() -> None:
    lern, client, laeufer = _aufbau()
    r = client.post(ANTWORT, json={"revision": 1, "answer": {"text": CODE}})
    assert r.status_code == 200, r.text
    assert laeufer.aufrufe == [(("python", CODE, ""), {})]
