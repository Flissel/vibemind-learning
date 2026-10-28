"""Werkstatt: the model's calculation is recomputed with SymPy in a child process, never eval'd here."""

from __future__ import annotations

import time

import pytest

from spaces.learning.services.werkstatt.nachrechnen import (
    ERLAUBTE_NAMEN,
    NachrechnenFehler,
    ausdruck_pruefen,
    nachrechnen,
)


def test_rechnet_ableitung_loesung_und_einsetzen() -> None:
    assert nachrechnen("solve(diff(x**3-3*x, x), x)[1]") == 1.0
    assert nachrechnen("(x**2-4).subs(x, 3)") == 5.0
    assert nachrechnen("2^3") == 8.0
    assert nachrechnen("Rational(1, 2) + sqrt(4)") == 2.5


@pytest.mark.parametrize(
    "ausdruck",
    [
        "__import__('os')",
        "open('a')",
        "x",  # no number
        "solve(x**2+1, x)[0]",  # complex
        "oo",  # not finite
        "solve(x**2-1, x)",  # a list, not a number
        "x.__class__",
        "eval(1)",
        "Symbol(1)",
        "1; 2",
        "lambda: 1",
        "",
    ],
)
def test_unerlaubt_oder_kein_zahlwert(ausdruck: str) -> None:
    with pytest.raises(NachrechnenFehler):
        nachrechnen(ausdruck)


def test_pruefung_vor_dem_parsen() -> None:
    assert ausdruck_pruefen("2^3 + x") == "2**3 + x"
    assert ausdruck_pruefen("1.5e3*x") == "1.5e3*x"
    assert {"x", "solve", "diff", "subs", "N"} <= ERLAUBTE_NAMEN
    for boese in ("__import__('os')", "open(1)", "a__b", "getattr(x, 1)", "x.func", "1 if 1 else 2", "{1}"):
        with pytest.raises(NachrechnenFehler):
            ausdruck_pruefen(boese)


def test_zeitlimit_bricht_ab() -> None:
    start = time.monotonic()
    with pytest.raises(NachrechnenFehler):
        nachrechnen("factor(x**99999-1)", timeout_s=3)
    assert time.monotonic() - start < 6
