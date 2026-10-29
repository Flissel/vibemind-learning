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
        "\\binom{n}{k}=\\frac{\\partial f}{\\partial x}+\\operatorname{sgn}(x)+\\arctan x$$\n"
        "und $\\mathbf{a}\\iff\\bar{x}\\subseteq\\lfloor y\\rfloor$ mit $\\mu,\\sigma,\\theta,\\lambda$ "
        "sowie $\\vec{v}=\\hat{u}$."
    )
    assert formel_fehler(text) == []


def test_rand_leerzeichen_in_satzformel() -> None:
    # Ruling 7: LearnHouse reads "$ x $" as text, so the check refuses it at the source.
    assert formel_fehler("Für $ x $ gilt das.") != []
    assert formel_fehler("Für $x $ gilt das.") != []
    assert formel_fehler("Für $ x$ gilt das.") != []
    assert formel_fehler("Für $x \\ne 1$ gilt das.") == []
    # A formula glued to a digit is no formula for LearnHouse either ("$x$5").
    assert formel_fehler("Mit $x$5 Euro") != []


def test_anzeigeformel_steht_auf_eigener_zeile() -> None:
    assert formel_fehler("Die Ableitung lautet $$f'(x)=3x^2$$ und so weiter.") != []
    assert formel_fehler("Die Ableitung lautet $$f'(x)=3x^2$$") != []
    assert formel_fehler("$$f'(x)=3x^2$$ ist die Ableitung.") != []
    assert formel_fehler("Die Ableitung lautet\n$$f'(x)=3x^2$$\nalso gilt") == []
    assert formel_fehler("Die Ableitung lautet\n  $$\n  f'(x)=3x^2\n  $$  \nalso gilt") == []


def test_lernskript_besteht_die_formelpruefung() -> None:
    from pathlib import Path

    skript = Path(__file__).resolve().parents[2] / "assets" / "kurse" / "kurvendiskussion" / "lernskript.md"
    assert formel_fehler(skript.read_text(encoding="utf-8")) == []


def test_code_zaehlt_nicht() -> None:
    assert formel_fehler("```bash\necho $HOME\n```") == []
    assert formel_fehler("Nutzen Sie `echo $HOME` hier.") == []
