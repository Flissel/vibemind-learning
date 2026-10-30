from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from spaces.learning.services.zeichnen.flaeche import (
    SKRIPT,
    flaeche_html,
    kriterien_anzeige,
    seite_html,
)

ERSATZSCHREIBUNGEN = (
    "Loesung", "Staerke", "Rueckgaengig", "loeschen", "Zeichenflaeche", "aenderbar",
    "einige Minuten", "Vorlaeufig", "verfuegbar", "ungueltig", "Pruefungsmodus", "prueft",
    "Zeichne deine", "deinem",
)


def _seite() -> str:
    return seite_html("Zeichenaufgabe", flaeche_html(""), nonce="n", skript=True).decode()


def test_page_has_proper_german_and_sie_form() -> None:
    seite = _seite()
    for falsch in ERSATZSCHREIBUNGEN:
        assert falsch not in seite, falsch
    assert not re.search(r"\b(deine[mnrs]?|du|dir|dich)\b", seite)
    for richtig in ("Lösung", "Stärke", "Rückgängig", "löschen", "Zeichenfläche", "änderbar", "einigen Minuten"):
        assert richtig in seite, richtig


def test_no_heading_and_compact_layout() -> None:
    seite = _seite()
    assert "<h1>" not in seite
    assert "min-height:60vh" in seite
    assert "Mehr anzeigen" in seite
    assert "-webkit-line-clamp:6" in seite
    assert 'width="1000" height="620"' in seite


def test_error_pages_keep_their_heading() -> None:
    assert b"<h1>Nicht gefunden</h1>" in seite_html("Nicht gefunden", "<p>x</p>", nonce="n")


def test_result_shows_description_not_id() -> None:
    ergebnis = {
        "session": {"feedback": {"criteria": [
            {"criterion_id": "r1", "description": "Parabel nach oben offen", "score": 1.0, "feedback": "ok"}]}},
    }
    bewertung = {"criteria": [
        {"criterion_id": "r1", "score": 1.0, "feedback": "Gut gezeichnet."},
        {"criterion_id": "r2", "score": 0.0, "feedback": "Fehlt."},
    ]}
    zeilen = kriterien_anzeige(bewertung, ergebnis)
    assert zeilen[0] == {"kriterium": "Parabel nach oben offen", "punkte": 1.0, "rueckmeldung": "Gut gezeichnet."}
    assert zeilen[1]["kriterium"] == "Kriterium 2"
    assert all("r1" not in z["kriterium"] and "r2" not in z["kriterium"] for z in zeilen)


def test_pointer_conversion_scales_by_css_size() -> None:
    assert "(e.clientX - r.left) * c.width / r.width" in SKRIPT
    assert "(e.clientY - r.top) * c.height / r.height" in SKRIPT
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    m = re.search(r"function punkt\(e\)\{.*?\n  return .*?;\s*\}", SKRIPT, re.S)
    assert m
    code = (
        "const c={width:1000,height:620,getBoundingClientRect:()=>({left:10,top:20,width:360,height:480})};"
        + m.group(0)
        + ";console.log(JSON.stringify(punkt({clientX:190,clientY:260})))"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[500,310]"


def test_rubric_evaluator_asks_for_plain_german_sie_feedback() -> None:
    from spaces.learning.services.course_factory.model_gateway import ROLE_PROMPTS

    satz = ("Write each criterion's feedback in plain German for the learner, addressed with 'Sie', "
            "without pixel coordinates, internal ids or image-processing terms.")
    assert ROLE_PROMPTS["rubric_evaluator"].endswith(satz)
