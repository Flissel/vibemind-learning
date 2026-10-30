from __future__ import annotations

from spaces.learning.services.werkstatt.formeltext import klartext
from spaces.learning.services.zeichnen.flaeche import flaeche_html


def test_klartext_symbole_und_bruch() -> None:
    assert klartext("für $x \\to \\infty$") == "für x → ∞"
    assert klartext("$f'(x)=\\frac{5}{(x+1)^2}$") == "f'(x)=5/(x+1)²"
    assert klartext("$\\frac{a+b}{c}$") == "(a+b)/c"
    assert klartext("$x_0 \\ge 1, x^5, \\mathbb{R}, \\left(a\\right) \\Rightarrow b$") == "x₀ ≥ 1, x^5, ℝ, (a) ⇒ b"
    assert klartext("Ableiten: Regeln, $f'$ und $f''$ nutzen") == "Ableiten: Regeln, f' und f'' nutzen"


def test_zeichenflaeche_klartext_nur_fuer_mathe() -> None:
    prompt = "Skizzieren Sie $x^2$."
    mathe = flaeche_html(prompt, mathe=True)
    assert "Skizzieren Sie x²." in mathe
    assert "$x^2$" in flaeche_html(prompt)
