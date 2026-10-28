"""Werkstatt: formula notation check."""

from __future__ import annotations

from spaces.learning.services.werkstatt.formeln import formel_fehler


def test_gueltige_formeln() -> None:
    assert formel_fehler("Die Ableitung ist $f'(x)=3x^2-3$.") == []
    assert formel_fehler("$$f(x)=\\frac{1}{x-1}$$") == []
    assert formel_fehler("$$\\begin{cases} 1 & x>0 \\\\ 0 & \\text{sonst} \\end{cases}$$") == []


def test_ungeschlossenes_dollar() -> None:
    assert formel_fehler("Kostet 5 $ im Laden") != []
    assert formel_fehler("Kostet 5 \\$ im Laden") == []


def test_klammern() -> None:
    assert formel_fehler("$\\frac{1}{2$") != []


def test_unbekannter_befehl_und_umgebung() -> None:
    assert formel_fehler("$\\foo x$") != []
    assert formel_fehler("$\\begin{matrix} 1 \\end{matrix}$") != []


def test_schulformeln_mit_erweiterten_befehlen() -> None:
    text = (
        "$$\\forall x\\in A:\\ \\lvert x\\rvert\\le\\max(1,2)\\implies "
        "\\binom{n}{k}=\\frac{\\partial f}{\\partial x}+\\operatorname{sgn}(x)+\\arctan x$$ "
        "und $\\mathbf{a}\\iff\\bar{x}\\subseteq\\lfloor y\\rfloor$ mit $\\mu,\\sigma,\\theta,\\lambda$ "
        "sowie $\\vec{v}=\\hat{u}$."
    )
    assert formel_fehler(text) == []


def test_code_zaehlt_nicht() -> None:
    assert formel_fehler("```bash\necho $HOME\n```") == []
    assert formel_fehler("Nutzen Sie `echo $HOME` hier.") == []
