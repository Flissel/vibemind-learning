"""„Kurs abgeben“ (ein Canvas je Aufgabe): Stand des Laufs und der Fall je Aufgabe."""

from __future__ import annotations

import pytest

from spaces.learning.huelle.kursabgabe import (
    UNLESBAR_EINE, UNLESBAR_MEHRERE, KursAbgabeStand, fall_fuer, nummernliste, unlesbar_text,
)


@pytest.mark.parametrize(("status", "hat_dokument", "fall"), [
    ("bestaetigt", True, "fertig"),
    ("bestaetigt", False, "fertig"),
    ("offen", False, "leer"),
    ("offen", True, "bewerten"),
])
def test_fall_fuer(status: str, hat_dokument: bool, fall: str) -> None:
    assert fall_fuer(status, hat_dokument) == fall


def test_stand_als_json() -> None:
    assert KursAbgabeStand("laeuft", 2, 5).als_json() == {
        "zustand": "laeuft", "aktuell": 2, "gesamt": 5, "meldung": None}
    assert KursAbgabeStand("fehler", 2, 5, "FLAECHE_BESCHAEFTIGT").als_json()["meldung"] == "FLAECHE_BESCHAEFTIGT"


def test_nummernliste() -> None:
    assert nummernliste([4]) == "4"
    assert nummernliste([6, 4]) == "4 und 6"
    assert nummernliste([2, 4, 6]) == "2, 4 und 6"


def test_unlesbar_text_eine_aufgabe() -> None:
    assert unlesbar_text([4]) == UNLESBAR_EINE.format(liste="4") == (
        "Aufgabe 4 konnte nicht gelesen werden. Bitte schreiben Sie deutlicher oder tippen Sie Ihre Antwort über "
        "„Falsch gelesen“ ein. Wenn Sie sie nicht bearbeiten möchten, drücken Sie noch einmal „Kurs abgeben“ – dann "
        "zählt sie als nicht bearbeitet.")


def test_unlesbar_text_mehrere_aufgaben() -> None:
    assert unlesbar_text([2, 4, 6]) == UNLESBAR_MEHRERE.format(liste="2, 4 und 6") == (
        "Aufgaben 2, 4 und 6 konnten nicht gelesen werden. Bitte schreiben Sie deutlicher oder tippen Sie Ihre "
        "Antworten über „Falsch gelesen“ ein. Wenn Sie sie nicht bearbeiten möchten, drücken Sie noch einmal "
        "„Kurs abgeben“ – dann zählen sie als nicht bearbeitet.")
    assert unlesbar_text([4, 6]).startswith("Aufgaben 4 und 6 konnten nicht gelesen werden.")
