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


@pytest.mark.parametrize(
    "ausdruck",
    [
        "9**9**9", "10^10^10", "2**-3**2", "9**(9**9)", "2**sqrt(2)**2", "x**2**3",
        "1" * 31, "1.5" + "0" * 31, "2**" + "9" * 31,
    ],
)
def test_vorab_grenzen_vor_dem_parsen(ausdruck: str) -> None:
    with pytest.raises(NachrechnenFehler):
        ausdruck_pruefen(ausdruck)


def test_einfache_potenzen_bleiben_erlaubt() -> None:
    assert ausdruck_pruefen("x**2 + x**3") == "x**2 + x**3"
    assert ausdruck_pruefen("(x**2)**3") == "(x**2)**3"
    assert ausdruck_pruefen("2^3 - 4^2") == "2**3 - 4**2"
    assert ausdruck_pruefen("1" * 30) == "1" * 30
    assert nachrechnen("(x**2-1).subs(x, 3)**2 + 2**3") == 72.0


def test_liste_wird_schnell_abgelehnt() -> None:
    start = time.monotonic()
    with pytest.raises(NachrechnenFehler):
        nachrechnen("[1]*10**8")
    assert time.monotonic() - start < 5


def test_speichergrenze_greift() -> None:
    # A 1.2 GB list: with a 3 GiB limit it is built and then rejected as "kein_zahlwert" (measured);
    # under the 1 GiB limit the child runs out of memory at once.
    start = time.monotonic()
    with pytest.raises(NachrechnenFehler, match="MemoryError"):
        nachrechnen("[1]*15*10**7", timeout_s=8)
    assert time.monotonic() - start < 6


def test_ohne_speichergrenze_wird_nicht_gerechnet() -> None:
    from spaces.learning.services.werkstatt.nachrechnen import _im_kind

    with pytest.raises(NachrechnenFehler, match="speichergrenze_fehlt"):
        _im_kind("1+1", timeout_s=10, speicher_grenze=0)
