"""„Kurs abgeben“ (ein Canvas je Aufgabe): Stand des Laufs und der Fall je Aufgabe."""

from __future__ import annotations

import pytest

from spaces.learning.huelle.kursabgabe import KursAbgabeStand, fall_fuer


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
