"""Recompute the model's calculation of a number task with SymPy, outside this process.

The calculation is model-written text. It is checked here against a character and a name
list, a digit limit and a ban on power towers before anything parses it, and it is parsed and
evaluated only in a child process (``python -I``) that first caps its own memory (fails closed
if it cannot) and then runs ``parse_expr`` restricted to the allowed names with no builtins,
under a hard time limit; the child is killed when the limit runs out. Nothing in this process
evaluates it.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys

ERLAUBTE_NAMEN: frozenset[str] = frozenset({
    "x", "diff", "solve", "subs", "sqrt", "exp", "log", "ln", "pi", "E", "Rational", "limit",
    "oo", "Abs", "sin", "cos", "tan", "simplify", "factor", "expand", "N",
})

SPEICHER_GRENZE = 1 << 30  # bytes the child may commit (Job Object on Windows, RLIMIT_AS elsewhere)
MAX_ZIFFERN = 30

_MAX_LAENGE = 2_000
_ZEICHEN = re.compile(r"[0-9A-Za-z_+\-*/^().,\[\]\s]*")
_ZU_VIELE_ZIFFERN = re.compile(r"\d{%d,}" % (MAX_ZIFFERN + 1))
# Numbers first, so the exponent of "1.5e3" is not read as a name "e3".
_ZAHL = r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_TOKEN = re.compile(rf"(?P<zahl>{_ZAHL})|(?P<name>{_NAME})")
_LEXEM = re.compile(rf"{_ZAHL}|{_NAME}|\*\*|[-+*/().,\[\]]")

# Runs in the child. The expression arrives on stdin, never inside this code. Before anything
# else the child caps its own memory; if that fails it refuses (never computes uncapped).
# `parse_expr` gets only the allowed names plus the three constructors its own number/symbol
# transformations emit (`Integer`, `Float`, `Symbol`; not reachable by name, since the name
# list has rejected them), and an empty `__builtins__` so `eval` cannot fall back to the real
# ones. The answer is one JSON line on stdout. `_KIND_KOPF` is shared by every task the child
# runs (here: a number; `graph.py`: drawing and relating graphs); each task appends its body.
_KIND_KOPF = r"""
import json, math, sys

def antwort(**werte):
    sys.stdout.write(json.dumps(werte) + "\n")
    sys.stdout.flush()
    sys.exit(0)

def grenze_setzen(grenze):
    if type(grenze) is not int or grenze <= 0:
        raise ValueError("grenze")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Basis(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class Erweitert(ctypes.Structure):
            _fields_ = [
                ("Basis", Basis), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.GetCurrentProcess.restype = wintypes.HANDLE
        job = k.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
        info = Erweitert()
        info.Basis.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = grenze
        # 9 = JobObjectExtendedLimitInformation
        if not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
        if not k.AssignProcessToJobObject(job, k.GetCurrentProcess()):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        return job  # the handle stays open for the life of the process
    import resource
    _weich, hart = resource.getrlimit(resource.RLIMIT_AS)
    neu = grenze if hart == resource.RLIM_INFINITY else min(grenze, hart)
    resource.setrlimit(resource.RLIMIT_AS, (neu, neu))
    if resource.getrlimit(resource.RLIMIT_AS) != (neu, neu):
        raise OSError("RLIMIT_AS")
    return None

try:
    auftrag = json.loads(sys.argv[1])
    _job = grenze_setzen(auftrag["grenze"])
except Exception:
    antwort(fehler="speichergrenze_fehlt")

def lesen(text):
    import sympy
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations
    erlaubt = {n: getattr(sympy, n) for n in auftrag["namen"] if n not in ("x", "subs")}
    erlaubt.update(Integer=sympy.Integer, Float=sympy.Float, Symbol=sympy.Symbol)
    erlaubt["__builtins__"] = {}
    return parse_expr(
        text, local_dict={"x": sympy.Symbol("x")}, global_dict=erlaubt,
        transformations=standard_transformations, evaluate=True,
    )
"""

_KIND_WERT = r"""
try:
    text = sys.stdin.read()
    import sympy
    ergebnis = lesen(text)
    # Type first: a list or anything else is never converted (sympify of a huge list is slow).
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


def _ist_name(lexem: str) -> bool:
    return lexem[0].isalpha() or lexem[0] == "_"


def _ist_zahl(lexem: str) -> bool:
    return lexem[0].isdigit() or (lexem[0] == "." and len(lexem) > 1)


def _lexeme(text: str) -> list[str]:
    out: list[str] = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        treffer = _LEXEM.match(text, pos)
        if treffer is None:
            raise NachrechnenFehler("zeichen_unerlaubt")
        out.append(treffer.group(0))
        pos = treffer.end()
    return out


def _hinter_klammer(lexeme: list[str], i: int) -> int:
    """Index after the bracket that closes the one opened at ``i``."""
    tiefe = 0
    for j in range(i, len(lexeme)):
        if lexeme[j] in "([":
            tiefe += 1
        elif lexeme[j] in ")]":
            tiefe -= 1
            if tiefe == 0:
                return j + 1
    raise NachrechnenFehler("klammern_offen")


def _operand_ende(lexeme: list[str], i: int) -> int:
    """Index after the operand of ``**`` starting at ``i`` (Python's power-operand grammar:
    unary signs, an atom, then calls, subscripts and attributes)."""
    while i < len(lexeme) and lexeme[i] in ("+", "-"):
        i += 1
    if i >= len(lexeme):
        raise NachrechnenFehler("operand_fehlt")
    if lexeme[i] in ("(", "["):
        i = _hinter_klammer(lexeme, i)
    elif _ist_name(lexeme[i]) or _ist_zahl(lexeme[i]):
        i += 1
    else:
        raise NachrechnenFehler("operand_fehlt")
    while i < len(lexeme):
        if lexeme[i] in ("(", "["):
            i = _hinter_klammer(lexeme, i)
        elif lexeme[i] == "." and i + 1 < len(lexeme) and _ist_name(lexeme[i + 1]):
            i += 2
        else:
            break
    return i


def _ohne_potenzturm(ausdruck: str) -> None:
    """No exponent may itself contain or be raised by a power: ``9**9**9``, ``2**-3**2`` and
    ``9**(9**9)`` are rejected, ``x**2 + x**3`` and ``(x**2)**3`` are fine."""
    lexeme = _lexeme(ausdruck)
    for i, lexem in enumerate(lexeme):
        if lexem != "**":
            continue
        ende = _operand_ende(lexeme, i + 1)
        if "**" in lexeme[i + 1:ende] or (ende < len(lexeme) and lexeme[ende] == "**"):
            raise NachrechnenFehler("potenzturm")


def ausdruck_pruefen(text: str, *, namen: frozenset[str] = ERLAUBTE_NAMEN) -> str:
    """The expression with ``^`` as ``**``, if it uses only allowed characters and names (out
    of ``namen``), no number longer than ``MAX_ZIFFERN`` digits and no power tower."""
    if not text.strip():
        raise NachrechnenFehler("leer")
    if len(text) > _MAX_LAENGE:
        raise NachrechnenFehler("zu_lang")
    if not _ZEICHEN.fullmatch(text):
        raise NachrechnenFehler("zeichen_unerlaubt")
    if "__" in text:
        raise NachrechnenFehler("unterstrich_unerlaubt")
    if _ZU_VIELE_ZIFFERN.search(text):
        raise NachrechnenFehler("zahl_zu_lang")
    for treffer in _TOKEN.finditer(text):
        name = treffer.group("name")
        if name is not None and name not in namen:
            raise NachrechnenFehler(f"name_unerlaubt:{name}")
    ausdruck = text.replace("^", "**")
    _ohne_potenzturm(ausdruck)
    return ausdruck


def _umgebung() -> dict[str, str]:
    # Nothing from this process's environment (keys, tokens); Windows needs SYSTEMROOT to start.
    return {k: os.environ[k] for k in ("SYSTEMROOT", "SystemRoot") if k in os.environ}


def im_kind_ausfuehren(
    rumpf: str, eingabe: str, *, namen: frozenset[str], timeout_s: float, speicher_grenze: int,
    umgebung: dict[str, str] | None = None,
) -> tuple[dict[str, object], int]:
    """Run ``_KIND_KOPF`` + ``rumpf`` (fixed code of this package) in the capped child with
    ``eingabe`` (checked model text, as JSON or plain) on stdin; returns the child's JSON answer
    (``{}`` if it gave none) and its exit code. ``namen``: what ``lesen`` may parse."""
    auftrag = json.dumps({"namen": sorted(namen), "grenze": speicher_grenze})
    try:
        lauf = subprocess.run(  # noqa: S603 - fixed argv, the model text goes via stdin
            [sys.executable, "-I", "-c", _KIND_KOPF + rumpf, auftrag],
            input=eingabe, capture_output=True, text=True, timeout=timeout_s,
            env={**_umgebung(), **(umgebung or {})},
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
    return antwort, lauf.returncode


def _im_kind(ausdruck: str, *, timeout_s: float, speicher_grenze: int) -> float:
    """Evaluate an already checked expression in the capped child process."""
    antwort, returncode = im_kind_ausfuehren(
        _KIND_WERT, ausdruck, namen=ERLAUBTE_NAMEN, timeout_s=timeout_s, speicher_grenze=speicher_grenze,
    )
    wert = antwort.get("wert")
    if isinstance(wert, bool) or not isinstance(wert, (int, float)) or not math.isfinite(wert):
        raise NachrechnenFehler(str(antwort.get("fehler") or f"kind_exit_{returncode}"))
    return float(wert)


def nachrechnen(rechnung: str, *, timeout_s: float = 10.0) -> float:
    """The value of the calculation as a finite real number; raises ``NachrechnenFehler``."""
    return _im_kind(ausdruck_pruefen(rechnung), timeout_s=timeout_s, speicher_grenze=SPEICHER_GRENZE)
