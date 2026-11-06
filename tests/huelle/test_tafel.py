"""Aufgabentext fuer das Board: Etiketten identisch zu contracts.beschriftung."""

from __future__ import annotations

import pytest

from spaces.learning.contracts.beschriftung import beschriften
from spaces.learning.huelle.tafel import aufgabentext, formel_oder_klartext, kuerzen, leistentext, rueckmeldungstext


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


def test_formel_bleibt_als_latex_erhalten() -> None:
    aufgabe = _aufgabe(
        "single_choice", prompt="Wie gross ist $\\frac{1}{2}+x^2$ und $$a \\le b$$?",
        options=[{"id": "a", "label": "$x \\to \\infty$"}])
    zeilen = aufgabentext(aufgabe, 1).splitlines()
    assert zeilen[0] == "Aufgabe 1: Wie gross ist $\\frac{1}{2}+x^2$ und $$a \\le b$$?"
    assert "A) $x \\to \\infty$" in zeilen


def test_zuordnen_und_reihenfolge_behalten_formeln() -> None:
    praes = {"left": [{"id": "l1", "label": "$x^2$"}], "right": [{"id": "r1", "label": "$\\sqrt{x}$"}]}
    zeilen = aufgabentext(_aufgabe("matching", presentation=praes), 1).splitlines()
    assert "1) $x^2$" in zeilen and "A) $\\sqrt{x}$" in zeilen
    zeilen = aufgabentext(_aufgabe("ordering", options=[{"id": "x", "label": "$a_1$"}]), 1).splitlines()
    assert "A) $a_1$" in zeilen


@pytest.mark.parametrize("roh", ["Preis 5 $ und 6 $", "Nur ein $ hier", "Kosten \\$5 und $x^2$", "$x$5 und $", "$ x $"])
def test_unausgewogenes_oder_preis_dollar_faellt_auf_klartext_zurueck(roh: str) -> None:
    aufgabe = _aufgabe("single_choice", prompt=roh, options=[{"id": "a", "label": roh}])
    zeilen = aufgabentext(aufgabe, 1).splitlines()
    assert "$" not in "\n".join(zeilen)
    assert zeilen[0].startswith("Aufgabe 1: ") and any(z.startswith("A) ") for z in zeilen)


def test_fallback_wandelt_formel_im_selben_text_in_klartext() -> None:
    text = aufgabentext(_aufgabe("open", prompt="Preis \\$5, $\\frac{1}{2}+x^2$ und $$a \\le b$$"), 1)
    assert text == "Aufgabe 1: Preis 5, 1/2+x² und a ≤ b"


@pytest.mark.parametrize("roh,erwartet,anzeige", [
    ("Es gilt $x^2$.", "Es gilt $x^2$.", True),
    ("$$a \\le b$$", "$$a \\le b$$", True),
    ("$$a \\le b$$", "a ≤ b", False),
    ("Ohne  Formel\t hier", "Ohne Formel hier", True),
    ("Preis 5 $ und 6 $", "Preis 5 und 6", True),
    ("$x$ und \\$5", "x und 5", True),
])
def test_formel_oder_klartext(roh: str, erwartet: str, anzeige: bool) -> None:
    assert formel_oder_klartext(roh, anzeige=anzeige) == erwartet


def test_beschriftung_unveraendert_bei_formeln() -> None:
    optionen = [{"id": f"o{i}", "label": f"$x_{i}$"} for i in range(5)]
    zeilen = aufgabentext(_aufgabe("multiple_choice", options=optionen), 1).splitlines()
    for etikett, oid in beschriften("multiple_choice", {}, optionen).optionen.items():
        assert f"{etikett}) $x_{oid[1:]}$" in zeilen


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


def test_rueckmeldungstext_formel_in_rueckmeldung_gelesen_bleibt_klartext() -> None:
    text = rueckmeldungstext("$x^2$", "Richtig.", "Es gilt $\\frac{1}{2}$.")
    assert text == "Gelesen: x²\n\nRichtig.\n\nEs gilt $\\frac{1}{2}$."


def test_rueckmeldungstext_fallback_und_kein_display() -> None:
    assert "$" not in rueckmeldungstext(None, "Richtig.", "Es gilt $\\frac{1}{2}$ und \\$5.")
    assert rueckmeldungstext(None, "Richtig.", "So: $$a \\le b$$ ok") == "Richtig.\n\nSo: a ≤ b ok"


def test_luecken_in_formeln_werden_gueltiges_tex() -> None:
    # Abnahme 06.11.: "___" in $..$ ist kein TeX (doppelter Index), PenEcho zeigte die Formel roh.
    roh = "zu $u^2 - ___ u + ___ = 0$ mit $u_1 = ___$ und Text ___ ohne Formel."
    text = formel_oder_klartext(roh)
    assert text == (
        r"zu $u^2 - \underline{\qquad} u + \underline{\qquad} = 0$ mit $u_1 = \underline{\qquad}$"
        " und Text ___ ohne Formel."
    )
    assert formel_oder_klartext(r"$a\_\_ = b_1$") == r"$a\_\_ = b_1$"
    assert formel_oder_klartext("$$x = ___$$") == r"$$x = \underline{\qquad}$$"


def test_leistentext_bleibt_kurz() -> None:
    # Abnahme 06.11. (F14): die volle Rubrik-Rueckmeldung ueberdeckte in der Leiste die Flaeche und "Passt".
    lang = 'Satz 0: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 1: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 2: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 3: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 4: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 5: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 6: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 7: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 8: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 9: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 10: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 11: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 12: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 13: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 14: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 15: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 16: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 17: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 18: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 19: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 20: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 21: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 22: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 23: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 24: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 25: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 26: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 27: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 28: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 29: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 30: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 31: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 32: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 33: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 34: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 35: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 36: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 37: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 38: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet. Satz 39: Die Ableitung ist an dieser Stelle noch nicht vollständig begründet.'
    text = leistentext("x", "Teilweise richtig.", lang)
    assert len(text) <= 240 and text.startswith("Gelesen: x Teilweise richtig. Satz 0:") and text.endswith(" …")
    assert leistentext(None, "Richtig.", "Gut.") == "Richtig. Gut."


def test_kuerzen_laesst_passenden_text_und_schneidet_an_wortgrenzen() -> None:
    assert kuerzen("kurz genug", zeichen=20) == "kurz genug"
    assert kuerzen("eins zwei drei vier", zeichen=12) == "eins zwei …"
    assert kuerzen("a\nb\nc\nd", zeichen=100, zeilen=2, je_zeile=10) == "a\nb …"
