"""Der Aufgabentyp code in Katalog, Vorschau und Autor (Werkstatt)."""

from __future__ import annotations

from typing import Any

from spaces.learning.services.evaluation.code_tests import fingerabdruck
from spaces.learning.services.werkstatt.autor import _ohne_testlauf
from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
from spaces.learning.services.werkstatt.katalog import _item_felder
from spaces.learning.services.werkstatt.vorschau import vorschau


def code(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "code", "prompt": "Geben Sie die Summe der Zahlen aus der Eingabe aus.", "concept_ids": ["k1"],
        "sprache": "python", "startcode": "import sys\n",
        "musterloesung": "import sys\nprint(sum(map(int, sys.stdin.read().split())))",
        "tests": [{"name": "drei Zahlen", "eingabe": "3 4 5", "erwartet": "12"},
                  {"name": "leer", "eingabe": "", "erwartet": "0"}],
        "expected_answer": "Liest die Zahlen und gibt ihre Summe aus.",
        "rubric": [{"criterion_id": "r1", "description": "Summe stimmt", "points": 2}],
    }
    base.update(over)
    return base


def _lauf(**over: Any) -> dict[str, Any]:
    i = inhalt_lesen(code())
    return {"fingerabdruck": fingerabdruck(i.sprache, i.musterloesung, i.tests), "bestanden": True,
            "fehlgeschlagen": [], **over}


def test_item_traegt_tests_verborgen_und_sprache_sichtbar() -> None:
    typ, erwartet, config, rubrik = _item_felder("item-1", inhalt_lesen(code()))
    assert typ == "code"
    assert erwartet == {"text": code()["musterloesung"]}
    assert config["prompt"] == code()["prompt"]
    # Nur `presentation` erreicht Lernende: Sprache und Startcode ja, Tests und Loesung nie.
    assert config["presentation"] == {"sprache": "python", "startcode": "import sys\n"}
    assert config["tests"] == code()["tests"]
    assert "tests" not in config["presentation"] and "musterloesung" not in str(config["presentation"])
    assert rubrik and rubrik[0]["criterion_id"] == "r1"


def test_vorschau_zeigt_sprache_startcode_und_den_stand_des_testlaufs() -> None:
    v = vorschau(code())
    assert "Sprache: Python" in v["text"] and "Startcode:" in v["text"] and "import sys" in v["text"]
    # Lernenden-Text ohne Loesung und ohne Tests.
    assert "sys.stdin.read" not in v["text"] and "drei Zahlen" not in v["text"]
    assert "Musterlösung:" in v["loesung"] and "drei Zahlen" in v["loesung"]
    assert "Testlauf: noch nicht gelaufen" in v["loesung"]
    assert "Testlauf: bestanden" in vorschau(code(testlauf=_lauf()))["loesung"]
    gescheitert = vorschau(code(testlauf=_lauf(bestanden=False, fehlgeschlagen=["leer"])))
    assert "Testlauf: nicht bestanden (leer)" in gescheitert["loesung"]
    assert "Startcode" not in vorschau(code(startcode=""))["text"]


def test_autor_verwirft_eine_behauptete_pruefmeldung() -> None:
    behauptet = inhalt_lesen(code(testlauf=_lauf()))
    assert _ohne_testlauf(behauptet).testlauf is None
    quiz = inhalt_lesen({"kind": "quiz", "prompt": "?", "concept_ids": ["k1"], "correct_option_ids": ["a"],
                         "options": [{"option_id": "a", "text": "A"}, {"option_id": "b", "text": "B"}]})
    assert _ohne_testlauf(quiz) is quiz


def test_erlaubnisse_und_dateien_an_der_aufgabe() -> None:
    import pytest
    from pydantic import ValidationError

    inhalt = inhalt_lesen(code(erlaubnisse=["pakete", "diagramm", "daten"],
                               dateien=[{"name": "umsatz.csv", "inhalt": "monat;umsatz\n1;100\n"}]))
    assert inhalt.erlaubnisse == ["pakete", "diagramm", "daten"] and inhalt.dateien[0].name == "umsatz.csv"
    assert inhalt_lesen(code()).erlaubnisse == [] and inhalt_lesen(code()).dateien == []
    for falsch in (
        code(erlaubnisse=["netz"]),
        code(erlaubnisse=["pakete", "pakete"]),
        code(dateien=[{"name": "a.csv", "inhalt": "1"}]),                        # Dateien nur mit "daten"
        code(erlaubnisse=["daten"], dateien=[{"name": "a.csv", "inhalt": "1"}, {"name": "a.csv", "inhalt": "2"}]),
        code(erlaubnisse=["daten"], dateien=[{"name": f"d{i}.csv", "inhalt": "1"} for i in range(6)]),
        code(erlaubnisse=["daten"], dateien=[{"name": f"d{i}.csv", "inhalt": "x" * 90_000} for i in range(3)]),
    ):
        with pytest.raises(ValidationError):
            inhalt_lesen(falsch)


def test_item_zeigt_erlaubnisse_und_dateinamen_die_inhalte_bleiben_verborgen() -> None:
    inhalt = inhalt_lesen(code(erlaubnisse=["pakete", "daten"],
                               dateien=[{"name": "umsatz.csv", "inhalt": "geheim;1\n"}]))
    _, _, config, _ = _item_felder("item-1", inhalt)
    assert config["presentation"] == {"sprache": "python", "startcode": "import sys\n",
                                      "erlaubnisse": ["pakete", "daten"], "dateien": ["umsatz.csv"]}
    assert config["dateien"] == [{"name": "umsatz.csv", "inhalt": "geheim;1\n"}]
    assert "geheim" not in str(config["presentation"])
    # Ohne Erlaubnisse bleibt die Darstellung wie bisher.
    _, _, ohne, _ = _item_felder("item-1", inhalt_lesen(code()))
    assert ohne["presentation"] == {"sprache": "python", "startcode": "import sys\n"} and "dateien" not in ohne


def test_pruefmeldung_gilt_nur_fuer_dieselben_erlaubnisse_und_dateien() -> None:
    from spaces.learning.services.werkstatt.pruefung import code_testlauf_codes

    ohne = inhalt_lesen(code(testlauf=_lauf()))
    assert code_testlauf_codes(ohne) == []
    # Dieselbe Pruefmeldung, aber die Aufgabe darf jetzt Pakete laden: der Lauf gilt nicht mehr.
    assert code_testlauf_codes(inhalt_lesen(code(testlauf=_lauf(), erlaubnisse=["pakete"]))) == ["code_ungeprueft"]


def test_vorschau_nennt_erlaubnisse_und_dateien() -> None:
    v = vorschau(code(erlaubnisse=["pakete", "diagramm"]))
    assert "Der Code darf: Pakete (numpy, matplotlib), Diagramm ausgeben" in v["text"]
    mit = vorschau(code(erlaubnisse=["daten"], dateien=[{"name": "umsatz.csv", "inhalt": "1;2"}]))
    assert "Übungsdaten lesen (umsatz.csv)" in mit["text"]
    assert "Der Code darf" not in vorschau(code())["text"]
