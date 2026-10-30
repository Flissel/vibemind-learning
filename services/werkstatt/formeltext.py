"""Plain text for names and drawing prompts: LaTeX formulas turned into readable Unicode."""

from __future__ import annotations

import re

_DOLLAR = re.compile(r"\$\$(.+?)\$\$|(?<!\\)\$([^$]+?)(?<!\\)\$", re.S)
_FRAC = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SYMBOLE = {
    "to": "→", "rightarrow": "→", "infty": "∞", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "ne": "≠", "neq": "≠", "cdot": "·", "pm": "±", "Rightarrow": "⇒", "Leftrightarrow": "⇔",
    "times": "×", "approx": "≈",
}
_HOCH = {"2": "²", "3": "³", "4": "⁴"}
_TIEF = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def _bruch(treffer: re.Match[str]) -> str:
    def teil(t: str) -> str:
        t = t.strip()
        tiefe, offen = 0, False
        for zeichen in t:
            tiefe += (zeichen == "(") - (zeichen == ")")
            if tiefe == 0 and zeichen in " +-*/":
                offen = True
        return f"({t})" if offen else t

    return f"{teil(treffer.group(1))}/{teil(treffer.group(2))}"


def _latex(formel: str) -> str:
    formel = re.sub(r"\\mathbb\{R\}", "ℝ", formel)
    formel = re.sub(r"\\mathbb\{([A-Za-z])\}", r"\1", formel)
    formel = re.sub(r"\\(?:left|right)(?![A-Za-z])\s*", "", formel)
    while _FRAC.search(formel):
        formel = _FRAC.sub(_bruch, formel)
    formel = re.sub(r"\\([A-Za-z]+)", lambda m: _SYMBOLE.get(m.group(1), m.group(1)), formel)
    formel = re.sub(r"\^\{?([234])\}?(?!\d)", lambda m: _HOCH[m.group(1)], formel)
    formel = re.sub(r"([A-Za-z])_\{?(\d+)\}?", lambda m: m.group(1) + m.group(2).translate(_TIEF), formel)
    return formel.replace("{", "").replace("}", "")


def klartext(text: str) -> str:
    """Strip `$` delimiters and turn LaTeX into Unicode; whitespace is collapsed."""
    text = _DOLLAR.sub(lambda m: _latex(m.group(1) or m.group(2)), text).replace("\\$", "$")
    return " ".join(text.split())
