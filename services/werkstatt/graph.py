"""Function graphs for matching tasks: drawn by the server, related with SymPy, outside this process.

A graph term is model-written text. Here it is only checked: characters, a name list without
anything that computes (no solve/diff/subs, only x, numbers, + - * / ^ ( ), sqrt, exp, ln/log,
sin/cos/tan, Abs, pi, E), digit limit and no power tower (``nachrechnen.ausdruck_pruefen``).
Parsing, ``lambdify``, evaluating and drawing (Matplotlib, Agg) happen only in the capped child
process of ``nachrechnen`` (``python -I``, memory capped before anything else, no builtins for
the parser, time limit, fails closed). The child sends back PNG bytes or the check result.

Learners get the picture and a neutral label, never the term.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from spaces.learning.services.werkstatt.inhalt import GraphAngabe
from spaces.learning.services.werkstatt.nachrechnen import (
    ERLAUBTE_NAMEN,
    SPEICHER_GRENZE,
    NachrechnenFehler,
    ausdruck_pruefen,
    im_kind_ausfuehren,
)

# Out of the calculation names (Task 3), only those that describe a function, none that computes.
GRAPH_NAMEN: frozenset[str] = frozenset({
    "x", "sqrt", "exp", "ln", "log", "sin", "cos", "tan", "Abs", "pi", "E",
})
assert GRAPH_NAMEN <= ERLAUBTE_NAMEN

BREITE, HOEHE = 480, 360
# Plot area in figure fractions (left, bottom, width, height); the rest holds arrows and numbers.
FLAECHE = (0.06, 0.06, 0.9, 0.9)
PUNKTE = 2_000
GRAPH_FARBE = "#1565c0"
# Measured (see the Task 4 report): sympy + numpy + matplotlib and drawing six graphs stay well
# below the calculation limit, so drawing uses the same cap.
SPEICHER_GRENZE_GRAPH = SPEICHER_GRENZE
ZEITLIMIT_S = 30.0

_ZEICHEN = re.compile(r"[0-9A-Za-z_+\-*/^().\s]*")
_ATTRIBUT = re.compile(r"\.\s*[A-Za-z_]")
_PNG_KOPF = b"\x89PNG\r\n\x1a\n"

# Child bodies, appended to ``nachrechnen._KIND_KOPF`` (memory cap, ``antwort``, ``lesen``).
_KIND_ZEICHNEN = r"""
try:
    angaben = json.loads(sys.stdin.read())
    import base64, io
    import numpy as np
    import sympy
    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcdefaults()
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter, MultipleLocator
    X = sympy.Symbol("x")
    BREITE, HOEHE, FLAECHE, PUNKTE, FARBE = auftrag_bild

    def schritt(spanne):
        for s in (1, 2, 5, 10, 20, 50, 100, 200, 500):
            if spanne / s <= 20:
                return s
        return 1000

    def werte(a):
        ausdruck = lesen(a["term"])
        if not isinstance(ausdruck, sympy.Expr) or not ausdruck.free_symbols <= {X}:
            antwort(fehler="kein_term")
        f = sympy.lambdify(X, ausdruck, modules="numpy")
        xs = np.linspace(a["x_min"], a["x_max"], PUNKTE)
        with np.errstate(all="ignore"):
            ys = np.asarray(f(xs))
            if np.iscomplexobj(ys):
                reell = np.abs(ys.imag) <= 1e-9 * np.maximum(1.0, np.abs(ys.real))
                ys = np.where(reell, ys.real, np.nan)
            ys = np.broadcast_to(ys.astype(float), xs.shape).copy()
            ys[~np.isfinite(ys)] = np.nan
            spanne = a["y_max"] - a["y_min"]
            # A jump larger than the whole window between two neighbours is a pole or a gap:
            # break the line there instead of drawing a vertical connection.
            sprung = np.abs(np.diff(ys)) > spanne
            ys[:-1][sprung] = np.nan
            ys[1:][sprung] = np.nan
            sichtbar = np.isfinite(ys) & (ys >= a["y_min"]) & (ys <= a["y_max"])
            if not sichtbar.any():
                antwort(fehler="nicht_sichtbar")
            ys = np.clip(ys, a["y_min"] - spanne, a["y_max"] + spanne)
        return xs, ys

    def zeichnen(a, xs, ys):
        fig = Figure(figsize=(BREITE / 100, HOEHE / 100), dpi=100, facecolor="white")
        FigureCanvasAgg(fig)
        ax = fig.add_axes(FLAECHE)
        ax.set_xlim(a["x_min"], a["x_max"])
        ax.set_ylim(a["y_min"], a["y_max"])
        ax.xaxis.set_major_locator(MultipleLocator(schritt(a["x_max"] - a["x_min"])))
        ax.yaxis.set_major_locator(MultipleLocator(schritt(a["y_max"] - a["y_min"])))
        ax.grid(True, color="#d9d9d9", linewidth=0.6)
        ax.set_axisbelow(True)
        for seite in ("top", "right"):
            ax.spines[seite].set_visible(False)
        x_achse = 0.0 if a["y_min"] <= 0 <= a["y_max"] else a["y_min"]
        y_achse = 0.0 if a["x_min"] <= 0 <= a["x_max"] else a["x_min"]
        ax.spines["bottom"].set_position(("data", x_achse))
        ax.spines["left"].set_position(("data", y_achse))
        for seite in ("bottom", "left"):
            ax.spines[seite].set_color("black")
            ax.spines[seite].set_linewidth(1.0)
        ax.plot([1], [x_achse], ">", color="black", markersize=5, transform=ax.get_yaxis_transform(), clip_on=False)
        ax.plot([y_achse], [1], "^", color="black", markersize=5, transform=ax.get_xaxis_transform(), clip_on=False)
        ohne_null = FuncFormatter(lambda v, _p: "" if abs(v) < 1e-9 else f"{v:g}")
        ax.xaxis.set_major_formatter(ohne_null)
        ax.yaxis.set_major_formatter(ohne_null)
        ax.tick_params(labelsize=7, length=3, colors="#333333")
        ax.plot(xs, ys, color=FARBE, linewidth=2)
        puffer = io.BytesIO()
        fig.savefig(puffer, format="png", dpi=100, facecolor="white", metadata={"Software": None})
        return base64.b64encode(puffer.getvalue()).decode("ascii")

    bilder = [zeichnen(a, *werte(a)) for a in angaben]
    antwort(bilder=bilder)
except Exception as fehler:
    antwort(fehler=type(fehler).__name__)
"""

_KIND_BEZIEHUNG = r"""
try:
    daten = json.loads(sys.stdin.read())
    import sympy
    X = sympy.Symbol("x")
    ergebnis = []
    for links, rechts in daten["paare"]:
        l, r = lesen(links), lesen(rechts)
        if not all(isinstance(t, sympy.Expr) and t.free_symbols <= {X} for t in (l, r)):
            antwort(fehler="kein_term")
        rest = sympy.diff(l, X) - r if daten["beziehung"] == "ableitung_von" else l - r
        ergebnis.append(bool(sympy.simplify(rest) == 0))
    antwort(ergebnis=ergebnis)
except Exception as fehler:
    antwort(fehler=type(fehler).__name__)
"""


class GraphFehler(ValueError):
    """A graph term is not allowed, cannot be drawn or shows nothing; the check reports
    ``graph_ungueltig``. ``grund`` says why (for logs, never for learners)."""

    code = "graph_ungueltig"

    def __init__(self, grund: str) -> None:
        super().__init__(grund)
        self.grund = grund


@dataclass(frozen=True)
class GezeichneterGraph:
    png: bytes
    width: int
    height: int


def term_pruefen(term: str) -> str:
    """The term with ``^`` as ``**`` if it only uses the graph characters and names."""
    if not _ZEICHEN.fullmatch(term):
        raise GraphFehler("zeichen_unerlaubt")
    if _ATTRIBUT.search(term):
        raise GraphFehler("attribut_unerlaubt")
    try:
        return ausdruck_pruefen(term, namen=GRAPH_NAMEN)
    except NachrechnenFehler as fehler:
        raise GraphFehler(str(fehler)) from fehler


def spalte_von(angabe: GraphAngabe, x: float) -> int:
    """Pixel column of ``x`` in the drawn picture."""
    links, _unten, breite, _hoehe = FLAECHE
    anteil = (x - angabe.x_min) / (angabe.x_max - angabe.x_min)
    return int((links + breite * anteil) * BREITE)


# NumPy's OpenBLAS reserves buffers per CPU thread at import; under the memory cap that aborts
# the child on a many-core host (measured: still failing at 512 MiB). One thread is plenty here.
_EIN_THREAD = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


def _zeichen_umgebung() -> dict[str, str]:
    """One BLAS thread, and a fixed Matplotlib config/cache folder (the child has no HOME):
    the font list is built once there instead of on every run."""
    for basis in (lambda: Path.home() / ".cache", lambda: Path(tempfile.gettempdir())):
        try:
            ordner = basis() / "learning-werkstatt-matplotlib"
            ordner.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError):
            continue
        return {**_EIN_THREAD, "MPLCONFIGDIR": str(ordner)}
    return dict(_EIN_THREAD)


def _png(roh: object) -> bytes:
    if not isinstance(roh, str):
        raise GraphFehler("kein_png")
    try:
        png = base64.b64decode(roh, validate=True)
    except (binascii.Error, ValueError) as fehler:
        raise GraphFehler("kein_png") from fehler
    groesse = (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big"))
    if not png.startswith(_PNG_KOPF) or png[12:16] != b"IHDR" or groesse != (BREITE, HOEHE):
        raise GraphFehler("kein_png")
    return png


def graphen_zeichnen(
    angaben: Sequence[GraphAngabe], *, timeout_s: float = ZEITLIMIT_S,
) -> list[GezeichneterGraph]:
    """All graphs in one child run (one start of Python, SymPy and Matplotlib)."""
    if not angaben:
        return []
    eingabe = json.dumps([
        {"term": term_pruefen(a.term), "x_min": a.x_min, "x_max": a.x_max, "y_min": a.y_min, "y_max": a.y_max}
        for a in angaben
    ])
    rumpf = f"auftrag_bild = {json.dumps([BREITE, HOEHE, list(FLAECHE), PUNKTE, GRAPH_FARBE])}\n" + _KIND_ZEICHNEN
    try:
        antwort, code = im_kind_ausfuehren(
            rumpf, eingabe, namen=GRAPH_NAMEN, timeout_s=timeout_s,
            speicher_grenze=SPEICHER_GRENZE_GRAPH, umgebung=_zeichen_umgebung(),
        )
    except NachrechnenFehler as fehler:
        raise GraphFehler(str(fehler)) from fehler
    bilder = antwort.get("bilder")
    if not isinstance(bilder, list) or len(bilder) != len(angaben):
        raise GraphFehler(str(antwort.get("fehler") or f"kind_exit_{code}"))
    return [GezeichneterGraph(_png(b), BREITE, HOEHE) for b in bilder]


def graph_zeichnen(angabe: GraphAngabe) -> GezeichneterGraph:
    """480x360 PNG: axes with arrows, grid every 1, the graph; no label naming the term."""
    return graphen_zeichnen([angabe])[0]


def beziehung_pruefen(
    paare: Sequence[tuple[str, str]], beziehung: Literal["ableitung_von", "gleich"],
    *, timeout_s: float = ZEITLIMIT_S,
) -> list[bool]:
    """Per pair of terms: is right the derivative of left (``simplify(diff(l, x) - r) == 0``),
    or equal to it (``simplify(l - r) == 0``)?"""
    if not paare:
        return []
    eingabe = json.dumps({
        "beziehung": beziehung, "paare": [[term_pruefen(l), term_pruefen(r)] for l, r in paare],
    })
    try:
        antwort, code = im_kind_ausfuehren(
            _KIND_BEZIEHUNG, eingabe, namen=GRAPH_NAMEN, timeout_s=timeout_s,
            speicher_grenze=SPEICHER_GRENZE_GRAPH, umgebung=dict(_EIN_THREAD),
        )
    except NachrechnenFehler as fehler:
        raise GraphFehler(str(fehler)) from fehler
    ergebnis = antwort.get("ergebnis")
    if not isinstance(ergebnis, list) or len(ergebnis) != len(paare) or any(type(e) is not bool for e in ergebnis):
        raise GraphFehler(str(antwort.get("fehler") or f"kind_exit_{code}"))
    return ergebnis
