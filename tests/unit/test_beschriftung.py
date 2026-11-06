from __future__ import annotations

import pytest

from spaces.learning.contracts.beschriftung import antwort_aus_etiketten, beschriften


def _optionen(count: int = 2) -> list[dict[str, object]]:
    return [{"id": f"o{index}", "label": f"Option {index}"} for index in range(count)]


def test_optionen_werden_in_angezeigter_reihenfolge_beschriftet() -> None:
    beschriftung = beschriften("single_choice", {}, [{"id": "a"}, {"id": "b"}])
    assert beschriftung.optionen == {"A": "a", "B": "b"}


def test_beschriftung_ist_deterministisch_und_unveraenderlich() -> None:
    erste = beschriften("single_choice", {}, _optionen(3))
    zweite = beschriften("single_choice", {}, _optionen(3))
    assert erste == zweite
    with pytest.raises(AttributeError):
        erste.optionen = {}  # type: ignore[misc]


def test_ab_27_optionen_value_error() -> None:
    assert len(beschriften("single_choice", {}, _optionen(26)).optionen) == 26
    with pytest.raises(ValueError):
        beschriften("single_choice", {}, _optionen(27))


def test_single_choice_antwort() -> None:
    b = beschriften("single_choice", {}, [{"id": "a"}, {"id": "b"}])
    assert antwort_aus_etiketten("single_choice", b, {"auswahl": ["B"]}) == {"choice_id": "b"}


def test_etikett_ohne_passende_option_ist_nicht_lesbar() -> None:
    b = beschriften("single_choice", {}, [{"id": "a"}, {"id": "b"}])
    assert antwort_aus_etiketten("single_choice", b, {"auswahl": ["E"]}) is None


def test_single_choice_braucht_genau_eine_auswahl() -> None:
    b = beschriften("single_choice", {}, [{"id": "a"}, {"id": "b"}])
    assert antwort_aus_etiketten("single_choice", b, {"auswahl": ["A", "B"]}) is None
    assert antwort_aus_etiketten("single_choice", b, {"auswahl": []}) is None
    assert antwort_aus_etiketten("single_choice", b, {}) is None


def test_multiple_choice_antwort_ohne_doppelte() -> None:
    b = beschriften("multiple_choice", {}, _optionen(3))
    assert antwort_aus_etiketten("multiple_choice", b, {"auswahl": ["C", "A"]}) == {
        "choice_ids": ["o2", "o0"]
    }
    assert antwort_aus_etiketten("multiple_choice", b, {"auswahl": ["A", "A"]}) is None


def test_matching_links_zahlen_rechts_buchstaben() -> None:
    presentation = {
        "left": [{"id": "l1"}, {"id": "l2"}],
        "right": [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}],
    }
    b = beschriften("matching", presentation, [])
    assert b.links == {"1": "l1", "2": "l2"}
    assert b.rechts == {"A": "r1", "B": "r2", "C": "r3"}
    assert antwort_aus_etiketten("matching", b, {"paare": {"1": "B"}}) == {"pairs": {"l1": "r2"}}
    assert antwort_aus_etiketten("matching", b, {"paare": {"1": "B", "2": "Z"}}) is None
    assert antwort_aus_etiketten("matching", b, {"paare": {"9": "B"}}) is None
    assert antwort_aus_etiketten("matching", b, {"paare": {}}) is None


def test_ordering_reihenfolge() -> None:
    b = beschriften("ordering", {}, _optionen(3))
    assert antwort_aus_etiketten("ordering", b, {"reihenfolge": ["C", "A", "B"]}) == {
        "order": ["o2", "o0", "o1"]
    }
    assert antwort_aus_etiketten("ordering", b, {"reihenfolge": ["C", "C", "B"]}) is None
    assert antwort_aus_etiketten("ordering", b, {"reihenfolge": []}) is None


def test_numeric_mit_unicode_minus_und_dezimalkomma() -> None:
    b = beschriften("numeric", {}, [])
    assert antwort_aus_etiketten("numeric", b, {"wert": "−1,5"}) == {"value": -1.5}
    assert antwort_aus_etiketten("numeric", b, {"wert": "-1.5"}) == {"value": -1.5}
    assert antwort_aus_etiketten("numeric", b, {"wert": 42}) == {"value": 42.0}
    assert antwort_aus_etiketten("numeric", b, {"wert": "1.234,5"}) == {"value": 1234.5}


@pytest.mark.parametrize(
    "wert", ["", "   ", "inf", "-inf", "nan", "NaN", "abc", "1,2,3", None, True]
)
def test_numeric_unlesbar_oder_nicht_endlich(wert: object) -> None:
    b = beschriften("numeric", {}, [])
    assert antwort_aus_etiketten("numeric", b, {"wert": wert}) is None


def test_numeric_nicht_endliche_zahl_als_float() -> None:
    b = beschriften("numeric", {}, [])
    assert antwort_aus_etiketten("numeric", b, {"wert": float("inf")}) is None
    assert antwort_aus_etiketten("numeric", b, {"wert": float("nan")}) is None


def test_fill_in_text() -> None:
    b = beschriften("fill_in", {}, [])
    assert antwort_aus_etiketten("fill_in", b, {"text": "  git status "}) == {"text": "git status"}
    assert antwort_aus_etiketten("fill_in", b, {"text": "   "}) is None


def test_unbekannter_aufgabentyp_ergibt_none() -> None:
    b = beschriften("penecho_canvas", {}, [])
    assert antwort_aus_etiketten("penecho_canvas", b, {"text": "x"}) is None


def test_einzelnes_etikett_als_text_gilt_als_einelementige_liste() -> None:
    b = beschriften("single_choice", {}, [{"id": "a"}, {"id": "b"}])
    assert antwort_aus_etiketten("single_choice", b, {"auswahl": "B"}) == {"choice_id": "b"}
    assert antwort_aus_etiketten("single_choice", b, {"auswahl": " b "}) == {"choice_id": "b"}
    assert antwort_aus_etiketten("single_choice", b, {"auswahl": ""}) is None
    assert antwort_aus_etiketten("single_choice", b, {"auswahl": "E"}) is None
    m = beschriften("multiple_choice", {}, _optionen(3))
    assert antwort_aus_etiketten("multiple_choice", m, {"auswahl": "C"}) == {"choice_ids": ["o2"]}


def test_gemeinsame_fixture_mit_der_seite() -> None:
    """Dieselbe Datei prueft huelle/seite/aufgabe.test.mjs: Etiketten und Korrektur-Antworten."""
    import json
    from pathlib import Path

    daten = json.loads((Path(__file__).parent.parent / "huelle" / "etiketten_fixture.json").read_text(encoding="utf-8"))
    for fall in daten["etiketten"]:
        task = fall["task"]
        b = beschriften(task["type"], task["presentation"], task["options"])
        assert {"optionen": b.optionen, "links": b.links, "rechts": b.rechts} == fall["erwartet"], fall["name"]
    for fall in daten["antworten"]:
        task = fall["task"]
        b = beschriften(task["type"], task["presentation"], task["options"])
        assert antwort_aus_etiketten(task["type"], b, fall["roh"]) == fall["antwort"], fall["name"]


def test_typenlisten_der_seite_stimmen_mit_learning_ueberein() -> None:
    """Die Typen der Fixture (von der Seite getestet) gleichen Learning und der Bruecke: kein stiller Drift."""
    import json
    import re
    from pathlib import Path

    from spaces.learning.huelle.bruecke import BEREICHS_TYPEN, DETERMINISTISCHE_TYPEN
    from spaces.learning.services.adaptive_engine import models

    typen = json.loads((Path(__file__).parent.parent / "huelle" / "etiketten_fixture.json").read_text(encoding="utf-8"))["typen"]
    quelle = Path(models.__file__).read_text(encoding="utf-8")
    # Die CHECK-Zeichenkette ist ueber zwei Literale verteilt: ab "item_type IN (" bis zur schliessenden Klammer.
    check = re.search(r"item_type IN \((.*?)\)\"", quelle, re.DOTALL)
    assert check is not None
    learning = set(re.findall(r"'([a-z_]+)'", check.group(1)))
    assert set(typen["alle"]) == learning
    assert set(typen["deterministisch"]) == set(DETERMINISTISCHE_TYPEN)
    assert set(typen["bereich"]) == set(BEREICHS_TYPEN)
    assert set(typen["seitenleiste"]) == learning - set(DETERMINISTISCHE_TYPEN) - set(BEREICHS_TYPEN)
