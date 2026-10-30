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
    assert "min-height" not in seite
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
    bewertung = {"accepted": True, "criteria": [
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


def test_canvas_box_ratio_equals_drawing_ratio_so_nothing_is_stretched() -> None:
    seite = _seite()
    css = re.search(r"canvas\{([^}]*)\}", seite).group(1)
    assert "height:auto" in css and "width:100%" in css and "min-height" not in css and "max-height" not in css
    assert "c.height = hoehe(c.clientWidth" in SKRIPT and "c.width =" not in SKRIPT
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    m = re.search(r"function hoehe.*", SKRIPT)
    assert m
    code = m.group(0) + ";console.log(JSON.stringify([hoehe(360,800),hoehe(1000,900),hoehe(300,100000)]))"
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, check=True).stdout
    tall, desktop, cap = __import__("json").loads(out)
    # 360x800 phone: box 360 wide, 45 % of 800 = 360 high; drawing 1000 x tall has the same ratio.
    assert abs(tall / 1000 - 360 / 360) < 0.01
    assert desktop == 620 and cap == 3000


def test_math_task_and_criteria_become_plain_text_everywhere() -> None:
    bewertung = {"accepted": True, "criteria": [{"criterion_id": "r1", "score": 1.0, "feedback": "Der Scheitel liegt bei $x^2$."}]}
    ergebnis = {"session": {"feedback": {"criteria": [
        {"criterion_id": "r1", "description": "Parabel $x^2$ offen", "score": 1.0, "feedback": ""}]}}}
    roh = kriterien_anzeige(bewertung, ergebnis)[0]
    klar = kriterien_anzeige(bewertung, ergebnis, mathe=True)[0]
    assert "$" in roh["kriterium"] and "$" not in klar["kriterium"] and "$" not in klar["rueckmeldung"]


def test_claw_page_gets_math_flag_and_umlaut_errors() -> None:
    from spaces.learning.claw.zeichnung import Zeichnung, ZeichnungAbgelehnt, objekte_aus_strichen

    assert Zeichnung("s", "a", "r", "t").mathe is False and Zeichnung("s", "a", "r", "t", mathe=True).mathe is True
    with pytest.raises(ZeichnungAbgelehnt) as fehler:
        objekte_aus_strichen("x")
    assert fehler.value.grund == "Die Zeichnung ist ungültig oder zu groß."


def test_criteria_are_hidden_while_the_evaluation_is_not_accepted() -> None:
    ergebnis = {"session": {"feedback": {"criteria": []}}}
    kriterien = [{"criterion_id": "r1", "score": 1.0, "feedback": "Gut."}]
    assert kriterien_anzeige({"accepted": False, "criteria": kriterien}, ergebnis) == []
    assert kriterien_anzeige({"criteria": kriterien}, ergebnis) == []
    assert len(kriterien_anzeige({"accepted": True, "criteria": kriterien}, ergebnis)) == 1


def test_toolbar_wraps_and_submit_sits_above_the_canvas() -> None:
    seite = _seite()
    css = re.search(r"\.leiste\{([^}]*)\}", seite).group(1)
    assert "flex-wrap:wrap" in css and "nowrap" not in css and "overflow-x" not in css
    leiste = seite[seite.index('class="leiste"'):seite.index("<canvas")]
    assert 'id="abgeben"' in leiste
