"""Testfaelle einer Programmieraufgabe: Vergleich, Fingerabdruck und Lauf (ohne echten Laeufer)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from spaces.learning.services.evaluation.code_tests import (
    TestErgebnis,
    Testfall,
    fingerabdruck,
    gleich,
    tests_laufen,
)

T1 = Testfall(name="drei Zahlen", eingabe="3 4 5\n", erwartet="12")
T2 = Testfall(name="leere Liste", eingabe="", erwartet="0")


@dataclass
class _Lauf:
    stdout: str
    exit_code: int | None = 0
    zeit_ueberschritten: bool = False


@pytest.mark.parametrize(
    ("ausgabe", "erwartet", "stimmt"),
    [
        ("12\n", "12", True),
        ("12\r\n", "12\n", True),
        ("a  \nb\t\n\n\n", "a\nb", True),
        ("12", "12 ", True),
        (" 12", "12", False),       # Leerraum am Zeilenanfang zaehlt
        ("a\n\nb", "a\nb", False),  # Leerzeile in der Mitte zaehlt
        ("12", "13", False),
        ("", "", True),
    ],
)
def test_gleich(ausgabe: str, erwartet: str, stimmt: bool) -> None:
    assert gleich(ausgabe, erwartet) is stimmt


def test_fingerabdruck_haengt_an_sprache_loesung_und_tests() -> None:
    basis = fingerabdruck("python", "print(1)", [T1, T2])
    assert len(basis) == 64 and basis == fingerabdruck("python", "print(1)", [T1, T2])
    assert basis != fingerabdruck("javascript", "print(1)", [T1, T2])
    assert basis != fingerabdruck("python", "print(2)", [T1, T2])
    assert basis != fingerabdruck("python", "print(1)", [T1])
    assert basis != fingerabdruck("python", "print(1)", [T1, T2.model_copy(update={"erwartet": "1"})])
    # Die Reihenfolge der Tests aendert nichts.
    assert basis == fingerabdruck("python", "print(1)", [T2, T1])


def test_tests_laufen_zaehlt_und_nennt_die_gescheiterten() -> None:
    aufrufe: list[tuple[str, str, str]] = []

    def ausfuehren(sprache: str, quelltext: str, eingabe: str) -> _Lauf:
        aufrufe.append((sprache, quelltext, eingabe))
        return _Lauf("12\n") if eingabe else _Lauf("7\n")

    ergebnis = tests_laufen("python", "code", [T1, T2], ausfuehren=ausfuehren)

    assert ergebnis == TestErgebnis(bestanden=1, gesamt=2, fehlgeschlagen=("leere Liste",))
    assert aufrufe == [("python", "code", "3 4 5\n"), ("python", "code", "")]


def test_absturz_und_zeitueberschreitung_zaehlen_als_gescheitert() -> None:
    laeufe = iter([_Lauf("12", exit_code=1), _Lauf("0", zeit_ueberschritten=True)])
    ergebnis = tests_laufen("python", "code", [T1, T2], ausfuehren=lambda *_: next(laeufe))
    assert ergebnis == TestErgebnis(bestanden=0, gesamt=2, fehlgeschlagen=("drei Zahlen", "leere Liste"))


def test_alle_bestanden() -> None:
    laeufe = iter([_Lauf("12"), _Lauf("0\n")])
    ergebnis = tests_laufen("javascript", "code", [T1, T2], ausfuehren=lambda *_: next(laeufe))
    assert ergebnis == TestErgebnis(bestanden=2, gesamt=2, fehlgeschlagen=())
    assert ergebnis.alle_bestanden


def test_ergebnis_muss_stimmig_sein() -> None:
    with pytest.raises(ValidationError):
        TestErgebnis(bestanden=3, gesamt=2, fehlgeschlagen=())
    with pytest.raises(ValidationError):
        TestErgebnis(bestanden=1, gesamt=2, fehlgeschlagen=())  # ein gescheiterter Test fehlt in der Liste
    assert not TestErgebnis(bestanden=0, gesamt=0, fehlgeschlagen=()).alle_bestanden  # ohne Tests nie bestanden


def test_testfall_grenzen() -> None:
    with pytest.raises(ValidationError):
        Testfall(name="", eingabe="", erwartet="1")
    with pytest.raises(ValidationError):
        Testfall(name="x" * 81, eingabe="", erwartet="1")
    with pytest.raises(ValidationError):
        Testfall(name="x", eingabe="e" * 4001, erwartet="1")


def test_fingerabdruck_schliesst_erlaubnisse_und_dateien_ein_aber_nur_wenn_es_welche_gibt() -> None:
    from spaces.learning.services.evaluation.code_tests import Datei

    ohne = fingerabdruck("python", "print(1)", [T1, T2])
    # Aufgaben ohne Erlaubnisse behalten ihren bisherigen Fingerabdruck (ihr Testlauf bleibt gueltig).
    assert ohne == fingerabdruck("python", "print(1)", [T1, T2], erlaubnisse=(), dateien=())
    mit = fingerabdruck("python", "print(1)", [T1, T2], erlaubnisse=("pakete", "daten"),
                        dateien=[Datei(name="a.csv", inhalt="1;2")])
    assert mit != ohne
    assert mit == fingerabdruck("python", "print(1)", [T1, T2], erlaubnisse=("daten", "pakete"),
                                dateien=[Datei(name="a.csv", inhalt="1;2")])
    assert mit != fingerabdruck("python", "print(1)", [T1, T2], erlaubnisse=("pakete", "daten"),
                                dateien=[Datei(name="a.csv", inhalt="1;3")])
    assert mit != fingerabdruck("python", "print(1)", [T1, T2], erlaubnisse=("pakete",))


def test_datei_grenzen() -> None:
    from spaces.learning.services.evaluation.code_tests import Datei

    assert Datei(name="umsatz_2026.csv", inhalt="a;b").name == "umsatz_2026.csv"
    for name in ("", "../a.csv", "a/b.csv", ".versteckt", "x" * 61, "a b.csv"):
        with pytest.raises(ValidationError):
            Datei(name=name, inhalt="1")
    with pytest.raises(ValidationError):
        Datei(name="a.csv", inhalt="x" * 100_001)
