"""Werkstatt content schemas and the rule that classifies a new version."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from spaces.learning.services.werkstatt.inhalt import (
    Grobentwurf,
    Quiz,
    fassungs_art,
    inhalt_lesen,
)

_UUID_A = "00000000-0000-4000-8000-000000000001"
_UUID_B = "00000000-0000-4000-8000-000000000002"
_UUID_C = "00000000-0000-4000-8000-000000000003"
_HASH = "a" * 64


def quiz(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "quiz",
        "prompt": "Welcher Befehl?",
        "concept_ids": ["k1"],
        "citation_ids": [],
        "options": [
            {"option_id": "a", "text": "Get-ChildItem"},
            {"option_id": "b", "text": "Get-Date"},
        ],
        "correct_option_ids": ["a"],
    }
    if "correct" in over:
        base["correct_option_ids"] = over.pop("correct")
    base.update(over)
    return base


def offen(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "open",
        "prompt": "Erklaeren Sie den Unterschied.",
        "concept_ids": ["k1"],
        "expected_answer": "Antwort A",
        "rubric": [{"criterion_id": "c1", "description": "Nennt A", "points": 2}],
    }
    base.update(over)
    return base


def luecke(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "cloze",
        "prompt": "Ergaenzen Sie.",
        "concept_ids": ["k1"],
        "template": "Get-{{b1}} -Path .",
        "blanks": [{"blank_id": "b1", "accepted": ["ChildItem"]}],
        "shell": "powershell",
    }
    base.update(over)
    return base


def test_quiz_ohne_richtige_option_ist_ungueltig() -> None:
    with pytest.raises(ValidationError):
        inhalt_lesen(quiz(correct=[]))


def test_luecke_platzhalter_muessen_zu_luecken_passen() -> None:
    with pytest.raises(ValidationError):
        inhalt_lesen(luecke(template="Get-{{b1}} {{b2}}"))
    assert inhalt_lesen(luecke()).kind == "cloze"


def test_inhalt_lesen_waehlt_typ_nach_kind() -> None:
    assert isinstance(inhalt_lesen(quiz()), Quiz)
    assert inhalt_lesen(offen()).kind == "open"
    with pytest.raises(ValidationError):
        inhalt_lesen({**quiz(), "kind": "unbekannt"})


def test_reihenfolge_und_zuordnung_grenzen() -> None:
    base = {"prompt": "p", "concept_ids": ["k1"]}
    assert inhalt_lesen({**base, "kind": "ordering", "items": ["a", "b", "c"]})
    with pytest.raises(ValidationError):
        inhalt_lesen({**base, "kind": "ordering", "items": ["a", "b"]})
    pairs = [{"left": str(i), "right": str(i)} for i in range(3)]
    assert inhalt_lesen({**base, "kind": "matching", "pairs": pairs})
    with pytest.raises(ValidationError):
        inhalt_lesen({**base, "kind": "matching", "pairs": pairs[:2]})


def test_grobentwurf_braucht_kapitel_mit_aufgaben() -> None:
    ok = {
        "schema_version": "werkstatt-grobentwurf-v1",
        "title": "T",
        "audience": "Einsteiger",
        "default_kind": "quiz",
        "chapters": [{"title": "K1", "tasks": [{"summary": "s"}]}],
    }
    assert Grobentwurf.model_validate(ok).chapters[0].tasks[0].summary == "s"
    with pytest.raises(ValidationError):
        Grobentwurf.model_validate({**ok, "chapters": [{"title": "K1", "tasks": []}]})


def test_fassungs_art_neu_bei_anderer_loesung() -> None:
    assert fassungs_art(quiz(correct=["a"]), quiz(correct=["b"])) == "neu"
    assert fassungs_art(offen(), offen(expected_answer="Antwort B")) == "neu"
    anders = luecke(blanks=[{"blank_id": "b1", "accepted": ["Item"]}])
    assert fassungs_art(luecke(), anders) == "neu"


def test_fassungs_art_neu_bei_geaenderten_optionstexten() -> None:
    opts = [
        {"option_id": "a", "text": "Get-ChildItem"},
        {"option_id": "b", "text": "Get-Process"},
    ]
    assert fassungs_art(quiz(), quiz(options=opts)) == "neu"


def test_fassungs_art_neu_bei_typwechsel_oder_anderen_konzepten() -> None:
    assert fassungs_art(quiz(), offen()) == "neu"
    assert fassungs_art(quiz(), quiz(concept_ids=["k2"])) == "neu"


def test_fassungs_art_textkorrektur_bei_nur_anderem_aufgabentext() -> None:
    assert (
        fassungs_art(quiz(prompt="Welcher Befehl?"), quiz(prompt="Welcher Befehl passt?"))
        == "textkorrektur"
    )


def test_fassungs_art_textkorrektur_bei_nur_anderen_zitaten() -> None:
    zitat = {
        "citation_id": _UUID_A,
        "source_id": _UUID_B,
        "source_revision": 1,
        "chunk_id": _UUID_C,
        "content_hash": _HASH,
        "locator": {"page": 1},
    }
    assert inhalt_lesen(quiz(zitate=[zitat])).zitate[0].citation_id == _UUID_A
    assert fassungs_art(quiz(), quiz(zitate=[zitat])) == "textkorrektur"


def test_fassungs_art_neu_ohne_vorgaenger() -> None:
    assert fassungs_art(None, quiz()) == "neu"


def test_fassungs_art_neu_bei_geaendertem_lueckentext() -> None:
    zwei = [
        {"blank_id": "b1", "accepted": ["ChildItem"]},
        {"blank_id": "b2", "accepted": ["Path"]},
    ]
    alt = luecke(template="Get-{{b1}} -{{b2}} .", blanks=zwei)
    vertauscht = luecke(template="Get-{{b2}} -{{b1}} .", blanks=zwei)
    assert fassungs_art(alt, vertauscht) == "neu"
    assert fassungs_art(luecke(), luecke(template="Remove-{{b1}} -Path .")) == "neu"


def test_fassungs_art_neu_bei_anderer_shell() -> None:
    assert fassungs_art(luecke(), luecke(shell="cmd")) == "neu"
    assert fassungs_art(luecke(), luecke(shell=None)) == "neu"
