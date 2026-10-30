"""Werkstatt: function graphs in matching tasks, drawn and related in the capped child process."""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from spaces.learning.services.werkstatt.freigabe import loesung_hash
from spaces.learning.services.werkstatt.graph import (
    BREITE,
    FLAECHE,
    GRAPH_NAMEN,
    HOEHE,
    GraphFehler,
    beziehung_pruefen,
    graph_zeichnen,
    graphen_zeichnen,
    spalte_von,
    term_pruefen,
)
from spaces.learning.services.werkstatt.inhalt import GraphAngabe, _loesung, inhalt_lesen
from spaces.learning.services.werkstatt.nachrechnen import ERLAUBTE_NAMEN


def _graphfarbe(pixel: tuple[int, ...]) -> bool:
    r, _g, b = pixel[:3]
    return b - r > 80 and b > 120


def _bild(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def test_zeichnet_png_in_fester_groesse_und_gleich_fuer_gleiche_angabe() -> None:
    graph = graph_zeichnen(GraphAngabe(term="x**2-1"))
    assert (graph.width, graph.height) == (BREITE, HOEHE) == (480, 360)
    bild = _bild(graph.png)
    assert bild.size == (480, 360)
    assert any(_graphfarbe(bild.getpixel((x, y))) for x in range(480) for y in range(0, 360, 3))
    assert graph_zeichnen(GraphAngabe(term="x**2-1")).png == graph.png
    assert graph_zeichnen(GraphAngabe(term="x^2-1")).png == graph.png


def test_polstelle_ohne_senkrechte_linie() -> None:
    angabe = GraphAngabe(term="1/(x-1)")
    bild = _bild(graph_zeichnen(angabe).png)

    def anteil(x: float) -> float:
        spalte = spalte_von(angabe, x)
        treffer = sum(_graphfarbe(bild.getpixel((spalte, y))) for y in range(360))
        return treffer / 360

    assert anteil(1.0) <= 0.10
    # The graph itself is drawn (left and right of the pole), so the check above is not vacuous.
    assert any(_graphfarbe(bild.getpixel((spalte_von(angabe, 3.0), y))) for y in range(360))
    assert any(_graphfarbe(bild.getpixel((spalte_von(angabe, -2.0), y))) for y in range(360))


def _luecke_probe(term: str) -> tuple[tuple[int, ...], bool]:
    angabe = GraphAngabe(term=term)
    bild = _bild(graph_zeichnen(angabe).png)
    # x=2 -> y=4 for (x^2-4)/(x-2); pixel row of y=4 in the plot area
    links, unten, breite, hoehe = FLAECHE
    zeile = int((1 - (unten + hoehe * (4 - angabe.y_min) / (angabe.y_max - angabe.y_min))) * HOEHE)
    mitte = bild.getpixel((spalte_von(angabe, 2.0), zeile))
    ring = any(
        _graphfarbe(bild.getpixel((spalte_von(angabe, 2.0) + dx, zeile + dy)))
        for dx in range(-8, 9) for dy in range(-8, 9) if 4 <= (dx * dx + dy * dy) ** 0.5 <= 8
    )
    return mitte, ring


def test_hebbare_luecke_ist_hohler_kreis() -> None:
    mitte, ring = _luecke_probe("(x^2-4)/(x-2)")
    assert mitte[:3] == (255, 255, 255)
    assert ring


def test_ohne_luecke_kein_kreis() -> None:
    for term in ("x+2", "1/(x-1)"):
        mitte, _ring = _luecke_probe(term)
        # the line (x+2) passes through; no white centre. For the pole the spot is empty background.
        assert not (term == "x+2" and mitte[:3] == (255, 255, 255))
    assert graph_zeichnen(GraphAngabe(term="(x^2-4)/(x-2)")).png == graph_zeichnen(GraphAngabe(term="(x^2-4)/(x-2)")).png


def test_tangens_und_wurzel_zeichnen_ohne_absturz() -> None:
    for term in ("tan(x)", "sqrt(x)", "ln(x)", "Abs(x) - 2", "exp(-x**2)*3", "2", "sin(pi*x)"):
        assert _bild(graph_zeichnen(GraphAngabe(term=term)).png).size == (480, 360)


@pytest.mark.parametrize(
    "term",
    [
        "__import__('os')", "open(1)", "diff(x**2, x)", "solve(x, x)", "x.subs(x, 1)", "[x]",
        "x, 1", "y + 1", "9**9**9", "lambda: 1", "Symbol(1)", "N(x)",
        "sqrt(x-10)",  # nothing finite in the window
        "x + 100",  # nothing inside the shown y range
    ],
)
def test_ungueltiger_term_ist_graph_ungueltig(term: str) -> None:
    with pytest.raises(GraphFehler) as fehler:
        graph_zeichnen(GraphAngabe(term=term))
    assert fehler.value.code == "graph_ungueltig"


def test_termpruefung_nur_mit_nicht_rechnenden_namen() -> None:
    assert GRAPH_NAMEN <= ERLAUBTE_NAMEN
    assert not {"diff", "solve", "subs", "limit", "simplify", "factor", "expand", "N", "Rational", "oo"} & GRAPH_NAMEN
    assert term_pruefen("x^3 - 2*x") == "x**3 - 2*x"
    with pytest.raises(GraphFehler):
        term_pruefen("diff(x**3, x)")


def test_mehrere_graphen_in_einem_lauf() -> None:
    graphen = graphen_zeichnen([GraphAngabe(term="x"), GraphAngabe(term="-x")])
    assert len(graphen) == 2 and graphen[0].png != graphen[1].png
    assert graphen_zeichnen([]) == []


def test_angabe_prueft_bereich() -> None:
    with pytest.raises(ValidationError):
        GraphAngabe(term="x", x_min=2, x_max=1)
    with pytest.raises(ValidationError):
        GraphAngabe(term="x", y_min=0, y_max=0)
    with pytest.raises(ValidationError):
        GraphAngabe(term="")
    with pytest.raises(ValidationError):
        GraphAngabe(term="x", x_max=float("inf"))
    standard = GraphAngabe(term="x")
    assert (standard.x_min, standard.x_max, standard.y_min, standard.y_max) == (-5, 5, -5, 5)


def test_beziehung_ableitung_und_gleich() -> None:
    assert beziehung_pruefen([("x**3", "3*x**2"), ("sin(x)", "cos(x)")], "ableitung_von") == [True, True]
    assert beziehung_pruefen([("x**3", "2*x")], "ableitung_von") == [False]
    assert beziehung_pruefen([("x^2-1", "(x-1)*(x+1)"), ("x", "2*x")], "gleich") == [True, False]
    with pytest.raises(GraphFehler):
        beziehung_pruefen([("__import__('os')", "x")], "gleich")


# --- schema and solution -----------------------------------------------------------------

_ZUORDNUNG: dict[str, Any] = {
    "kind": "matching", "prompt": "Ordnen Sie jedem Befehl seine Wirkung zu.", "concept_ids": ["k4"],
    "pairs": [
        {"left": "ls", "right": "listet Dateien"}, {"left": "cd", "right": "wechselt den Ordner"},
        {"left": "pwd", "right": "zeigt den Ordner"}, {"left": "mkdir", "right": "legt einen Ordner an"},
    ],
}


def _mit_graphen(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "matching", "prompt": "Ordnen Sie jedem Graphen den Graphen seiner Ableitung zu.",
        "concept_ids": ["k1"], "beziehung": "ableitung_von",
        "pairs": [
            {"left": "f", "right": "f'", "left_graph": {"term": "x**3"}, "right_graph": {"term": "3*x**2"}},
            {"left": "g", "right": "g'", "left_graph": {"term": "sin(x)"}, "right_graph": {"term": "cos(x)"}},
            {"left": "h", "right": "h'", "left_graph": {"term": "exp(x)"}, "right_graph": {"term": "exp(x)"}},
        ],
    }
    base.update(over)
    return base


def test_loesung_ohne_graphen_bleibt_byte_gleich() -> None:
    # Pinned before graphs existed (Ruling 2): released plain matching tasks keep their hash.
    assert _loesung(inhalt_lesen(_ZUORDNUNG)) == [
        ("cd", "wechselt den Ordner"), ("ls", "listet Dateien"),
        ("mkdir", "legt einen Ordner an"), ("pwd", "zeigt den Ordner"),
    ]
    assert loesung_hash(_ZUORDNUNG) == "c45f128e1b07a4b66bc5e5c6abab2f778b6f711c784d215b98735e990a4d57e7"


def test_loesung_mit_graphen_schliesst_terme_und_beziehung_ein() -> None:
    basis = loesung_hash(_mit_graphen())
    anders = _mit_graphen()
    anders["pairs"][0]["right_graph"] = {"term": "3*x**2", "y_max": 10}
    assert loesung_hash(anders) != basis
    anders = _mit_graphen()
    anders["pairs"][1]["left_graph"] = {"term": "-sin(x)"}
    assert loesung_hash(anders) != basis
    assert loesung_hash(_mit_graphen(beziehung="gleich")) != basis
    assert loesung_hash(_mit_graphen(beziehung=None)) != basis
    assert loesung_hash(_mit_graphen(prompt="Ordnen Sie zu.")) == basis


def test_beziehung_braucht_graphen_auf_beiden_seiten() -> None:
    ohne = _mit_graphen()
    del ohne["pairs"][2]["right_graph"]
    with pytest.raises(ValidationError):
        inhalt_lesen(ohne)
    del ohne["beziehung"]
    assert inhalt_lesen(ohne).pairs[2].right_graph is None
