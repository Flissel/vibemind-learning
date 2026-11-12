"""Kursrueckmeldung am Kursende (ein Canvas je Aufgabe): Eingabe fuer die Flaechen-KI, Antwort, Zahlen-Rueckfall."""

from __future__ import annotations

from typing import Any

import pytest

from spaces.learning.huelle.kursrueckmeldung import aus_antwort, aus_zahlen, als_text, rueckmeldungsdaten
from spaces.learning.huelle.zusammenfassung import AufgabeZeile, Kursrueckmeldung

MUSTER = "MUSTERLOESUNG-NIE-SENDEN"


def _zeile(n: int, *, richtig: bool = False, **kw: Any) -> AufgabeZeile:
    werte: dict[str, Any] = {"text": f"Frage {n}", "ergebnis": str(n), "kernaussage": "Richtig. Gut.", "bild": None,
                             "ki_schritte": ("solve", "hint"), "richtig": richtig}
    werte.update(kw)
    return AufgabeZeile(n, **werte)


def test_rueckmeldungsdaten_schema() -> None:
    lang = "x" * 5000
    zeilen = [_zeile(1, text=lang, ergebnis=lang, kernaussage=lang, ki_schritte=("solve",) * 80),
              *(_zeile(n) for n in range(2, 106))]
    begriffe = [{"name": "y" * 300, "stand": "z" * 80, "musterloesung": MUSTER}] + \
               [{"name": f"B{i}", "stand": "80 %"} for i in range(120)]
    daten = rueckmeldungsdaten("K" * 300, zeilen, begriffe)
    assert set(daten) == {"kurs", "aufgaben", "begriffe"}
    assert len(daten["kurs"]) == 200
    assert 1 <= len(daten["aufgaben"]) <= 100 and len(daten["begriffe"]) == 100
    for a in daten["aufgaben"]:
        assert set(a) == {"nummer", "titel", "ergebnis", "rueckmeldung", "ki_schritte"}
        assert isinstance(a["nummer"], int) and a["nummer"] >= 1
        assert len(a["titel"]) <= 200 and len(a["ergebnis"]) <= 200 and len(a["rueckmeldung"]) <= 2000
        assert len(a["ki_schritte"]) <= 50 and all(isinstance(s, str) and len(s) <= 40 for s in a["ki_schritte"])
    erste = daten["aufgaben"][0]
    assert len(erste["titel"]) == 200 and len(erste["ergebnis"]) == 200 and len(erste["rueckmeldung"]) == 2000
    assert len(erste["ki_schritte"]) == 50
    assert daten["aufgaben"][1]["ki_schritte"] == ["Lösen", "Hinweis"]
    for b in daten["begriffe"]:
        assert set(b) == {"name", "stand"} and len(b["name"]) <= 200 and len(b["stand"]) <= 40
    assert MUSTER not in repr(daten) and "musterloesung" not in repr(daten)


def test_rueckmeldungsdaten_ohne_aufgaben_und_kaputte_begriffe() -> None:
    daten = rueckmeldungsdaten("Kurs", [], [{"name": 3, "stand": "x"}, "kein Mapping"])  # type: ignore[list-item]
    assert daten == {"kurs": "Kurs", "aufgaben": [], "begriffe": []}


def test_aus_antwort_verlangt_ki_flaeche() -> None:
    gut = {"grader": "ki_flaeche", "staerken": "Brüche sicher.", "luecken": "Geraden.", "naechste_schritte": "Üben."}
    assert aus_antwort(gut) == Kursrueckmeldung("Brüche sicher.", "Geraden.", "Üben.", "ki_flaeche")
    assert aus_antwort(None) is None
    assert aus_antwort({**gut, "grader": "server"}) is None
    assert aus_antwort({**gut, "luecken": "  "}) is None
    assert aus_antwort({**gut, "staerken": 3}) is None
    assert aus_antwort({k: v for k, v in gut.items() if k != "naechste_schritte"}) is None
    assert aus_antwort(["kein", "objekt"]) is None  # type: ignore[arg-type]


def test_aus_zahlen_texte() -> None:
    k = aus_zahlen([_zeile(1, richtig=True), _zeile(2, richtig=True), _zeile(3)])
    assert k == Kursrueckmeldung("2 von 3 Aufgaben richtig.", "Offen: Aufgabe 3.",
                                 "Wiederholen Sie die offenen Aufgaben.", "zahlen")
    assert aus_zahlen([_zeile(1), _zeile(2, richtig=True), _zeile(4)]).luecken == "Offen: Aufgabe 1, 4."
    alle = aus_zahlen([_zeile(1, richtig=True)])
    assert (alle.staerken, alle.luecken, alle.naechste_schritte) == (
        "1 von 1 Aufgaben richtig.", "Keine.", "Weiter so.")


def test_als_text_fuer_die_karte() -> None:
    text = als_text(Kursrueckmeldung("Gut *fett*", "Lücke", "Üben", "ki_flaeche"))
    assert text.splitlines()[0] == "## Kursrückmeldung"
    assert "**Stärken:** Gut \\*fett\\*" in text and "**Lücken:** Lücke" in text
    assert "**Nächste Schritte:** Üben" in text


@pytest.mark.parametrize("feld", ["staerken", "luecken", "naechste_schritte"])
def test_aus_antwort_kuerzt_auf_2000(feld: str) -> None:
    wert = {"grader": "ki_flaeche", "staerken": "a", "luecken": "b", "naechste_schritte": "c", feld: "x" * 3000}
    k = aus_antwort(wert)
    assert k is not None and len(getattr(k, feld)) == 2000
