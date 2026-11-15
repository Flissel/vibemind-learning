"""Berichtstext am Kursende."""

from __future__ import annotations

from spaces.learning.huelle.bericht import abschluss_text, bericht_text, urteile, versuche_text

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


def test_urteil_der_engine_gilt_vor_den_alten_grenzen() -> None:
    # Die Engine liefert das Urteil je Aufgabe mit (Rubrik-Antworten sind ab 0,7 richtig, feste nur vollstaendig).
    s = {"attempted": 4, "correct": 2, "partial": 1, "score": 0.6, "results": [
        {"ordinal": 1, "item_id": "a", "score": 0.7, "state": "correct"},
        {"ordinal": 2, "item_id": "b", "score": 0.83, "state": "partial"},
        {"ordinal": 3, "item_id": "c", "score": 0.5, "state": "incorrect"},
        {"ordinal": 4, "item_id": "d", "score": 1.0, "state": "correct"},
    ]}
    text = bericht_text("K", s, [])
    assert "**2 von 4 richtig, 1 teilweise**" in text
    assert "Aufgabe 1: richtig" in text and "Aufgabe 2: teilweise" in text and "Aufgabe 3: falsch" in text
    assert urteile(s) == [(1, "richtig"), (2, "teilweise"), (3, "falsch"), (4, "richtig")]
    assert abschluss_text("K", s).splitlines()[1] == "2 von 4 Aufgaben richtig, 1 teilweise."


def test_ohne_teilweise_bleibt_die_kopfzeile_kurz() -> None:
    s = {"results": [{"ordinal": 1, "score": 1.0, "state": "correct"}, {"ordinal": 2, "score": 0.0, "state": "incorrect"}]}
    assert "**1 von 2 richtig**" in bericht_text("K", s, [])
    assert abschluss_text("K", s).splitlines()[1] == "1 von 2 Aufgaben richtig."


def test_unbekanntes_urteil_faellt_auf_die_grenzen_zurueck() -> None:
    s = {"results": [{"ordinal": 1, "score": 1.0, "state": "wunderbar"}, {"ordinal": 2, "score": 0.2, "state": 7}]}
    assert urteile(s) == [(1, "richtig"), (2, "falsch")]


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


def test_versuche_stehen_neben_dem_ergebnis_das_zaehlt() -> None:
    # Es zaehlt der beste Versuch; der Bericht nennt, wie viele es waren und wie der erste ausging.
    s = {"results": [
        {"ordinal": 1, "score": 1.0, "state": "correct"},
        {"ordinal": 2, "score": 1.0, "state": "correct"},
        {"ordinal": 3, "score": 0.5, "state": "partial"},
        {"ordinal": 4, "score": 1.0, "state": "correct"},
    ]}
    text = bericht_text("K", s, [], versuche={1: (1, "correct"), 2: (3, "incorrect"), 3: (2, "partial"), 4: (2, None)})
    assert "- Aufgabe 1: richtig\n" in text
    assert "- Aufgabe 2: richtig (3 Versuche, zuerst falsch)" in text
    assert "- Aufgabe 3: teilweise (2 Versuche)" in text  # gleich geblieben: kein „zuerst“
    assert "- Aufgabe 4: richtig (2 Versuche)" in text


def test_versuche_text() -> None:
    assert versuche_text(0, None) == "" and versuche_text(1, "incorrect") == ""
    assert versuche_text(2, "incorrect", "richtig") == "2 Versuche, zuerst falsch"
    assert versuche_text(2, "needs_review", "richtig") == "2 Versuche"
