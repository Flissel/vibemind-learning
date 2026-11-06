"""Aufgabentext fuer das Board: Etiketten identisch zu contracts.beschriftung."""

from __future__ import annotations

import pytest

from spaces.learning.contracts.beschriftung import beschriften
from spaces.learning.huelle.tafel import aufgabentext, rueckmeldungstext


def _aufgabe(typ: str, **mehr: object) -> dict[str, object]:
    basis: dict[str, object] = {"type": typ, "prompt": "Was gilt?", "options": [], "presentation": {}}
    basis.update(mehr)
    return basis


def test_auswahl_hat_zeilen_a_und_b_in_optionsreihenfolge() -> None:
    aufgabe = _aufgabe("single_choice", options=[{"id": "z9", "label": "Zweiter"}, {"id": "a1", "label": "Erster"}])
    zeilen = aufgabentext(aufgabe, 4).splitlines()
    assert zeilen[0] == "Aufgabe 4: Was gilt?"
    assert "A) Zweiter" in zeilen and "B) Erster" in zeilen
    assert zeilen.index("A) Zweiter") < zeilen.index("B) Erster")


def test_etiketten_sind_die_der_beschriftung() -> None:
    optionen = [{"id": f"o{i}", "label": f"Text{i}"} for i in range(5)]
    zeilen = aufgabentext(_aufgabe("multiple_choice", options=optionen), 1).splitlines()
    for etikett, oid in beschriften("multiple_choice", {}, optionen).optionen.items():
        assert f"{etikett}) Text{oid[1:]}" in zeilen


def test_zuordnen_links_zahlen_rechts_buchstaben() -> None:
    praes = {"left": [{"id": "l1", "label": "Hund"}, {"id": "l2", "label": "Katze"}],
             "right": [{"id": "r1", "label": "bellt"}, {"id": "r2", "label": "miaut"}]}
    zeilen = aufgabentext(_aufgabe("matching", presentation=praes), 2).splitlines()
    for zeile in ("1) Hund", "2) Katze", "A) bellt", "B) miaut"):
        assert zeile in zeilen
    assert zeilen.index("2) Katze") < zeilen.index("A) bellt")


def test_reihenfolge_hat_buchstaben() -> None:
    aufgabe = _aufgabe("ordering", options=[{"id": "x", "label": "Erst"}, {"id": "y", "label": "Dann"}])
    zeilen = aufgabentext(aufgabe, 1).splitlines()
    assert "A) Erst" in zeilen and "B) Dann" in zeilen


@pytest.mark.parametrize("typ", ["open", "numeric", "fill_in", "drawing", "essay"])
def test_ohne_etikettenblock(typ: str) -> None:
    aufgabe = _aufgabe(typ, options=[{"id": "x", "label": "Nicht anzeigen"}])
    assert aufgabentext(aufgabe, 7) == "Aufgabe 7: Was gilt?"


def test_kein_dollar_und_formeln_im_klartext() -> None:
    aufgabe = _aufgabe(
        "single_choice", prompt="Wie gross ist $\\frac{1}{2}+x^2$? Preis \\$5 und $$a \\le b$$",
        options=[{"id": "a", "label": "$x \\to \\infty$"}])
    text = aufgabentext(aufgabe, 1)
    assert "$" not in text
    assert "1/2" in text and "x²" in text and "≤" in text and "A) x → ∞" in text


def test_mehrzeiliger_prompt_bleibt_mehrzeilig() -> None:
    text = aufgabentext(_aufgabe("open", prompt="Zeile eins\nZeile zwei"), 3)
    assert text.splitlines() == ["Aufgabe 3: Zeile eins", "Zeile zwei"]


def test_zu_viele_optionen_wirft() -> None:
    aufgabe = _aufgabe("single_choice", options=[{"id": f"o{i}", "label": "t"} for i in range(27)])
    with pytest.raises(ValueError):
        aufgabentext(aufgabe, 1)


def test_rueckmeldungstext_mit_und_ohne_gelesen() -> None:
    assert rueckmeldungstext("B", "Richtig.", "Gut.") == "Gelesen: B\n\nRichtig.\n\nGut."
    assert rueckmeldungstext(None, "Nicht lesbar.", "Bitte deutlicher.") == "Nicht lesbar.\n\nBitte deutlicher."
    assert rueckmeldungstext("  ", "Richtig.", "") == "Richtig."


def test_rueckmeldungstext_ohne_dollar() -> None:
    assert "$" not in rueckmeldungstext("$x^2$", "Richtig.", "Es gilt $\\frac{1}{2}$ und \\$5.")
