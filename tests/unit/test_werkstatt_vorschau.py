from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spaces.learning.services.werkstatt.vorschau import vorschau


def _quiz() -> dict[str, Any]:
    return {
        "kind": "quiz", "prompt": "Welcher Befehl prueft, ob eine Datei existiert?",
        "concept_ids": ["k1"],
        "options": [
            {"option_id": "a", "text": "Get-Date"},
            {"option_id": "b", "text": "Test-Path"},
            {"option_id": "c", "text": "Get-Help"},
        ],
        "correct_option_ids": ["b"],
    }


def _luecke(shell: str | None) -> dict[str, Any]:
    return {
        "kind": "cloze", "prompt": "Ergaenzen Sie den Befehl.", "concept_ids": ["k1"],
        "template": "Test-Path {{b1}}", "shell": shell,
        "blanks": [{"blank_id": "b1", "accepted": ["C:/Daten/bericht.txt", "bericht.txt"]}],
    }


def _reihenfolge() -> dict[str, Any]:
    return {
        "kind": "ordering", "prompt": "Bringen Sie die Schritte in Reihenfolge.",
        "concept_ids": ["k1"], "items": ["eins", "zwei", "drei", "vier", "fuenf", "sechs"],
    }


def test_quiz_vorschau_zeigt_optionen_ohne_markierung_und_loesung_getrennt() -> None:
    ergebnis = vorschau(_quiz())
    assert "A) Get-Date" in ergebnis["text"]
    assert "B) Test-Path" in ergebnis["text"]
    assert "C) Get-Help" in ergebnis["text"]
    assert "Richtig" not in ergebnis["text"]
    assert ergebnis["loesung"] == "Richtig: B"
    assert ergebnis["bild"] is None
    assert ergebnis["lektion"] is None


def test_luecke_mit_shell_hat_bild_wenn_medienordner_gesetzt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    ergebnis = vorschau(_luecke("powershell"))
    assert ergebnis["bild"] is not None
    assert ergebnis["bild"].startswith("/lernsequenz-medien/term-")
    assert "____" in ergebnis["text"]
    assert "C:/Daten/bericht.txt" in ergebnis["loesung"]
    assert list((tmp_path / "lernsequenzen" / "medien").glob("term-*.png"))


def test_luecke_ohne_medienordner_hat_kein_bild(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEARNING_ARTIFACT_ROOT", raising=False)
    assert vorschau(_luecke("powershell"))["bild"] is None


def test_luecke_ohne_shell_hat_kein_bild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    assert vorschau(_luecke(None))["bild"] is None


def test_reihenfolge_ist_gemischt_und_stabil() -> None:
    erster = vorschau(_reihenfolge())
    zweiter = vorschau(_reihenfolge())
    assert erster["text"] == zweiter["text"]
    zeilen = [z.split(". ", 1)[1] for z in erster["text"].splitlines() if z[:1].isdigit()]
    assert sorted(zeilen) == sorted(_reihenfolge()["items"])
    assert zeilen != _reihenfolge()["items"]
    assert erster["loesung"].startswith("Richtige Reihenfolge: eins")


def test_zuordnung_rechts_gemischt_und_loesung_mit_paaren() -> None:
    inhalt = {
        "kind": "matching", "prompt": "Ordnen Sie zu.", "concept_ids": ["k1"],
        "pairs": [{"left": f"L{i}", "right": f"R{i}"} for i in range(1, 7)],
    }
    ergebnis = vorschau(inhalt)
    assert "L1 -> R1" in ergebnis["loesung"]
    assert "L1" in ergebnis["text"] and "R6" in ergebnis["text"]
    assert vorschau(inhalt) == ergebnis


def test_vorschau_zeigt_lektionsabschnitt() -> None:
    inhalt = {**_quiz(), "lektion": {"title": "Dateien pruefen", "body": "Mit Test-Path pruefen Sie."}}
    assert vorschau(inhalt)["lektion"] == "Dateien pruefen\n\nMit Test-Path pruefen Sie."


def test_reihenfolge_vorschau_weicht_immer_von_loesung_ab() -> None:
    for i in range(60):
        items = [f"a{i}", f"b{i}", f"c{i}"]
        inhalt = {"kind": "ordering", "prompt": f"P{i}", "concept_ids": ["k1"], "items": items}
        zeilen = [z.split(". ", 1)[1] for z in vorschau(inhalt)["text"].splitlines() if z[:1].isdigit()]
        assert sorted(zeilen) == sorted(items)
        assert zeilen != items


def test_zuordnung_rechts_nie_in_loesungsreihenfolge() -> None:
    for i in range(60):
        paare = [{"left": f"L{j}-{i}", "right": f"R{j}-{i}"} for j in range(3)]
        inhalt = {"kind": "matching", "prompt": f"P{i}", "concept_ids": ["k1"], "pairs": paare}
        rechts = [z.split(") ", 1)[1] for z in vorschau(inhalt)["text"].splitlines() if z[:2] in ("A)", "B)", "C)")]
        assert sorted(rechts) == sorted(p["right"] for p in paare)
        assert rechts != [p["right"] for p in paare]


def test_bildfehler_laesst_textvorschau_stehen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from spaces.learning.services.werkstatt import vorschau as modul

    def kaputt(*_a: object, **_k: object) -> None:
        raise RuntimeError("kein Font")

    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(modul, "render_terminal", kaputt)
    inhalt = {**_luecke("powershell"), "lektion": {"title": "T", "body": "B"}}
    ergebnis = vorschau(inhalt)
    assert ergebnis["bild"] is None
    assert "____" in ergebnis["text"] and ergebnis["loesung"] and ergebnis["lektion"] == "T\n\nB"


def test_zahl_vorschau_zeigt_eingabefeld_und_loesung_mit_rechnung() -> None:
    ergebnis = vorschau({
        "kind": "number", "prompt": "Berechnen Sie die Minimalstelle.", "concept_ids": ["k1"],
        "value": 1, "tolerance": 0.05, "label": "x =", "rechnung": "solve(diff(x**3-3*x, x), x)[1]",
    })
    assert ergebnis["text"] == "Berechnen Sie die Minimalstelle.\n\nx = ____"
    assert ergebnis["loesung"] == "Lösung: 1 (± 0.05); Rechnung: solve(diff(x**3-3*x, x), x)[1]"


def _graph_zuordnung() -> dict[str, Any]:
    return {
        "kind": "matching", "prompt": "Ordnen Sie jedem Graphen seine Ableitung zu.", "concept_ids": ["k1"],
        "beziehung": "ableitung_von",
        "pairs": [
            {"left": "f", "right": "f'", "left_graph": {"term": "x**3"}, "right_graph": {"term": "3*x**2"}},
            {"left": "g", "right": "g'", "left_graph": {"term": "sin(x)"}, "right_graph": {"term": "cos(x)"}},
            {"left": "h", "right": "h'", "left_graph": {"term": "x**2"}, "right_graph": {"term": "2*x"}},
        ],
    }


def test_zuordnung_mit_graphen_zeigt_bild_statt_term(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    ergebnis = vorschau(_graph_zuordnung())
    assert "Graph A" in ergebnis["text"] and "Graph 1" in ergebnis["text"]
    assert "x**3" not in ergebnis["text"] and "sin" not in ergebnis["text"]
    assert [b["label"] for b in ergebnis["bilder"][:3]] == ["Graph A", "Graph B", "Graph C"]
    assert sorted(b["label"] for b in ergebnis["bilder"][3:]) == ["Graph 1", "Graph 2", "Graph 3"]
    assert all(b["bild"].startswith("/lernsequenz-medien/term-") for b in ergebnis["bilder"])
    # Author-facing: the solution names the terms and the relation.
    assert "x**3" in ergebnis["loesung"] and "3*x**2" in ergebnis["loesung"]
    assert "Ableitung" in ergebnis["loesung"]
    assert vorschau(_graph_zuordnung()) == ergebnis


def test_zuordnung_mit_graphen_ohne_medienordner_platzhalter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEARNING_ARTIFACT_ROOT", raising=False)
    ergebnis = vorschau(_graph_zuordnung())
    assert all(b["bild"] is None for b in ergebnis["bilder"]) and len(ergebnis["bilder"]) == 6
    assert "x**3" not in ergebnis["text"]


def test_zuordnung_ohne_graphen_hat_keine_bilderliste() -> None:
    inhalt = {
        "kind": "matching", "prompt": "Ordnen Sie zu.", "concept_ids": ["k1"],
        "pairs": [{"left": f"L{i}", "right": f"R{i}"} for i in range(1, 4)],
    }
    assert "bilder" not in vorschau(inhalt)


def test_skizze_vorschau_zeigt_aufgabe_und_kriterien() -> None:
    ergebnis = vorschau({
        "kind": "sketch", "prompt": "Skizzieren Sie $f(x)=x^2$.", "concept_ids": ["k1"],
        "expected_answer": "Nach oben offene Parabel.",
        "rubric": [
            {"criterion_id": "r1", "description": "Parabel nach oben offen", "points": 2},
            {"criterion_id": "r2", "description": "Scheitel im Ursprung", "points": 2},
            {"criterion_id": "r3", "description": "Achsen beschriftet", "points": 1},
        ],
    })
    assert ergebnis["text"] == "Skizzieren Sie $f(x)=x^2$."
    assert "Nach oben offene Parabel." in ergebnis["loesung"]
    assert "Scheitel im Ursprung (2)" in ergebnis["loesung"]
