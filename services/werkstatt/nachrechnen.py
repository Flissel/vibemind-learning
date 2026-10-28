"""Recompute the model's calculation of a number task with SymPy, outside this process.

The calculation is model-written text. It is checked here against a character and a name
list before anything parses it, and it is parsed and evaluated only in a child process
(``python -I``) with ``parse_expr`` restricted to the allowed names, no builtins, and a hard
time limit; the child is killed when the limit runs out. Nothing in this process evaluates it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ERLAUBTE_NAMEN: frozenset[str] = frozenset({
    "x", "diff", "solve", "subs", "sqrt", "exp", "log", "ln", "pi", "E", "Rational", "limit",
    "oo", "Abs", "sin", "cos", "tan", "simplify", "factor", "expand", "N",
})

_MAX_LAENGE = 2_000
_ZEICHEN = re.compile(r"[0-9A-Za-z_+\-*/^().,\[\]\s]*")
# Numbers first, so the exponent of "1.5e3" is not read as a name "e3".
_TOKEN = re.compile(
    r"(?P<zahl>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)

# Runs in the child. The expression arrives on stdin, never inside this code. `parse_expr`
# gets only the allowed names plus the three constructors its own number/symbol
# transformations emit (`Integer`, `Float`, `Symbol`; not reachable by name, since the name
# list has rejected them), and an empty `__builtins__` so `eval` cannot fall back to the real
# ones. The answer is one JSON line on stdout.
_KIND = r"""
import json, math, sys
try:
    import resource
    _grenze = 1 << 30
    resource.setrlimit(resource.RLIMIT_AS, (_grenze, _grenze))
except Exception:
    pass

def antwort(**werte):
    sys.stdout.write(json.dumps(werte) + "\n")
    sys.exit(0)

try:
    namen = json.loads(sys.argv[1])
    text = sys.stdin.read()
    import sympy
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations
    x = sympy.Symbol("x")
    erlaubt = {n: getattr(sympy, n) for n in namen if n not in ("x", "subs")}
    erlaubt.update(Integer=sympy.Integer, Float=sympy.Float, Symbol=sympy.Symbol)
    erlaubt["__builtins__"] = {}
    ergebnis = parse_expr(
        text, local_dict={"x": x}, global_dict=erlaubt,
        transformations=standard_transformations, evaluate=True,
    )
    ergebnis = sympy.sympify(ergebnis)
    if not isinstance(ergebnis, sympy.Expr) or ergebnis.free_symbols:
        antwort(fehler="kein_zahlwert")
    wert = complex(sympy.N(ergebnis, 30))
    if not (math.isfinite(wert.real) and math.isfinite(wert.imag)):
        antwort(fehler="nicht_endlich")
    if abs(wert.imag) > 1e-12 * max(1.0, abs(wert.real)):
        antwort(fehler="nicht_reell")
    antwort(wert=wert.real)
except Exception as fehler:
    antwort(fehler=type(fehler).__name__)
"""


class NachrechnenFehler(ValueError):
    """The calculation is not allowed, ran out of time or has no finite real number."""


def ausdruck_pruefen(text: str) -> str:
    """The expression with ``^`` as ``**``, if it uses only allowed characters and names."""
    if not text.strip():
        raise NachrechnenFehler("leer")
    if len(text) > _MAX_LAENGE:
        raise NachrechnenFehler("zu_lang")
    if not _ZEICHEN.fullmatch(text):
        raise NachrechnenFehler("zeichen_unerlaubt")
    if "__" in text:
        raise NachrechnenFehler("unterstrich_unerlaubt")
    for treffer in _TOKEN.finditer(text):
        name = treffer.group("name")
        if name is not None and name not in ERLAUBTE_NAMEN:
            raise NachrechnenFehler(f"name_unerlaubt:{name}")
    return text.replace("^", "**")


def _umgebung() -> dict[str, str]:
    # Nothing from this process's environment (keys, tokens); Windows needs SYSTEMROOT to start.
    return {k: os.environ[k] for k in ("SYSTEMROOT", "SystemRoot") if k in os.environ}


def nachrechnen(rechnung: str, *, timeout_s: float = 10.0) -> float:
    """The value of the calculation as a finite real number; raises ``NachrechnenFehler``."""
    ausdruck = ausdruck_pruefen(rechnung)
    try:
        lauf = subprocess.run(  # noqa: S603 - fixed argv, the expression goes via stdin
            [sys.executable, "-I", "-c", _KIND, json.dumps(sorted(ERLAUBTE_NAMEN))],
            input=ausdruck, capture_output=True, text=True, timeout=timeout_s,
            env=_umgebung(),
        )
    except subprocess.TimeoutExpired as fehler:
        # subprocess.run has killed the child before raising.
        raise NachrechnenFehler("zeitlimit") from fehler
    zeilen = lauf.stdout.strip().splitlines()
    try:
        antwort = json.loads(zeilen[-1]) if zeilen else {}
    except ValueError:
        antwort = {}
    if not isinstance(antwort, dict):
        antwort = {}
    wert = antwort.get("wert")
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        raise NachrechnenFehler(str(antwort.get("fehler") or f"kind_exit_{lauf.returncode}"))
    return float(wert)
