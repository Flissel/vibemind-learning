"""Berichtstext am Kursende."""

from __future__ import annotations

from spaces.learning.huelle.bericht import bericht_text

SUMMARY = {
    "attempted": 3, "correct": 2, "score": 0.63,
    "results": [
        {"ordinal": 1, "item_id": "a", "score": 1.0},
        {"ordinal": 2, "item_id": "b", "score": 0.7},
        {"ordinal": 3, "item_id": "c", "score": 0.2},
    ],
}
BEGRIFFE = [{"concept_key": "k1", "titel": "Bruchrechnen", "beherrschung": 0.756},
            {"concept_key": "k2", "titel": "Ohne Stand", "beherrschung": None}]


def test_urteile_aus_den_score_grenzen() -> None:
    text = bericht_text("Mathe", SUMMARY, BEGRIFFE)
    assert "Aufgabe 1: richtig" in text
    assert "Aufgabe 2: teilweise" in text
    assert "Aufgabe 3: falsch" in text


def test_grenzen_wie_learning() -> None:
    s = {"attempted": 3, "correct": 1, "score": 0.5, "results": [
        {"ordinal": 1, "item_id": "a", "score": 0.999},
        {"ordinal": 2, "item_id": "b", "score": 0.65},
        {"ordinal": 3, "item_id": "c", "score": 0.6499},
    ]}
    text = bericht_text("K", s, [])
    assert "Aufgabe 1: richtig" in text and "Aufgabe 2: teilweise" in text and "Aufgabe 3: falsch" in text


def test_kopfzeile_zaehlt_wie_die_liste() -> None:
    # Learnings `correct` zaehlt ab 0,65 (hier 2); die Liste zeigt nur ein "richtig": die Kopfzeile folgt der Liste.
    text = bericht_text("Mathe", SUMMARY, BEGRIFFE)
    assert "1 von 3 richtig" in text and "2 von 3" not in text


def test_markdown_in_titel_und_begriffen_wird_entschaerft() -> None:
    text = bericht_text("A *fett*\n# Zwei", SUMMARY, [{"titel": "x_y\n[l](u)", "beherrschung": 0.5}])
    assert text.splitlines()[0] == "# Bericht: A \\*fett\\* \\# Zwei"
    assert "- x\\_y \\[l\\](u): 50 %" in text


def test_titel_und_begriffe_mit_prozent() -> None:
    text = bericht_text("Mathe", SUMMARY, BEGRIFFE)
    assert text.startswith("# Bericht: Mathe")
    assert "Bruchrechnen: 76 %" in text
    assert "Ohne Stand" not in text


def test_keine_anzahl_von_anlaeufen() -> None:
    text = bericht_text("Mathe", {**SUMMARY, "attempts": 9}, BEGRIFFE)
    assert "Anl" not in text and "Versuch" not in text
