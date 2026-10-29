"""Werkstatt content schemas and the rule that classifies a new version."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from spaces.learning.services.werkstatt.inhalt import (
    Grobentwurf,
    Quiz,
    Zahl,
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


def zahl(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "number",
        "prompt": "Berechnen Sie die Stelle $x$, an der $f(x)=x^3-3x$ ein Minimum hat.",
        "concept_ids": ["k1"],
        "value": 1.0,
        "rechnung": "solve(diff(x**3-3*x, x), x)[1]",
        "label": "x =",
    }
    base.update(over)
    return base


def test_zahl_wird_gelesen_mit_vorgaben() -> None:
    inhalt = inhalt_lesen(zahl())
    assert isinstance(inhalt, Zahl)
    assert (inhalt.value, inhalt.tolerance, inhalt.label) == (1.0, 0.01, "x =")
    with pytest.raises(ValidationError):
        inhalt_lesen(zahl(rechnung=""))
    with pytest.raises(ValidationError):
        inhalt_lesen(zahl(tolerance=-1))
    with pytest.raises(ValidationError):
        inhalt_lesen(zahl(label="x" * 41))
    with pytest.raises(ValidationError):
        inhalt_lesen(zahl(value=float("nan")))


def test_grobentwurf_kennt_zahl() -> None:
    entwurf = {
        "schema_version": "werkstatt-grobentwurf-v1", "title": "T", "audience": "A",
        "default_kind": "number", "chapters": [{"title": "K1", "tasks": [{"summary": "s"}]}],
    }
    assert Grobentwurf.model_validate(entwurf).default_kind == "number"


def test_fassungs_art_zahl() -> None:
    assert fassungs_art(zahl(), zahl(value=2.0)) == "neu"
    assert fassungs_art(zahl(), zahl(tolerance=0.5)) == "neu"
    assert fassungs_art(zahl(), zahl(label="y =")) == "neu"
    assert fassungs_art(zahl(), zahl(prompt="Wo hat $f$ ein Minimum?")) == "textkorrektur"


def skizze(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "sketch", "prompt": "Skizzieren Sie $f(x)=x^2$.", "concept_ids": ["k1"],
        "expected_answer": "Nach oben offene Parabel mit Scheitel im Ursprung.",
        "rubric": [
            {"criterion_id": "r1", "description": "Parabel nach oben offen", "points": 2},
            {"criterion_id": "r2", "description": "Scheitel im Ursprung", "points": 2},
            {"criterion_id": "r3", "description": "Achsen beschriftet", "points": 1},
        ],
    }
    base.update(over)
    return base


def test_skizze_braucht_drei_bis_fuenf_kriterien() -> None:
    from spaces.learning.services.werkstatt.inhalt import Skizze

    assert isinstance(inhalt_lesen(skizze()), Skizze)
    with pytest.raises(ValidationError):
        inhalt_lesen(skizze(rubric=skizze()["rubric"][:2]))
    with pytest.raises(ValidationError):
        inhalt_lesen(skizze(rubric=skizze()["rubric"] * 2))
    with pytest.raises(ValidationError):
        inhalt_lesen(skizze(expected_answer=""))


def test_grobentwurf_kennt_skizze() -> None:
    entwurf = {
        "schema_version": "werkstatt-grobentwurf-v1", "title": "T", "audience": "A",
        "default_kind": "sketch", "chapters": [{"title": "K1", "tasks": [{"summary": "s"}]}],
    }
    assert Grobentwurf.model_validate(entwurf).default_kind == "sketch"


def test_fassungs_art_skizze() -> None:
    kriterien = skizze()["rubric"]
    assert fassungs_art(skizze(), skizze(expected_answer="Andere Musterskizze")) == "neu"
    assert fassungs_art(skizze(), skizze(rubric=[*kriterien[:2], {**kriterien[2], "points": 3}])) == "neu"
    assert fassungs_art(skizze(), skizze(rubric=list(reversed(kriterien)))) == "textkorrektur"
    assert fassungs_art(skizze(), skizze(prompt="Zeichnen Sie $f(x)=x^2$.")) == "textkorrektur"
