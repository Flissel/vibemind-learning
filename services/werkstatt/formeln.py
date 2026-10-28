"""Fixed check of formula notation in task texts: `$...$` inline, `$$...$$` as a block, LaTeX as
KaTeX reads it. No model, no rendering: only balanced dollars and braces and known commands."""

from __future__ import annotations

import re

ERLAUBTE_BEFEHLE: frozenset[str] = frozenset(
    "frac dfrac sqrt cdot times div pm mp le leq ge geq ne neq approx infty lim to rightarrow "
    "Rightarrow Leftrightarrow left right sum int ln log exp sin cos tan pi alpha beta gamma "
    "Delta delta varepsilon mathbb mathrm text quad qquad underbrace overline in notin cup cap "
    "setminus emptyset mid ldots cdots prime begin end cases".split()
)
_UMGEBUNGEN = {"cases", "aligned"}

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_BLOCK = re.compile(r"\$\$(.+?)\$\$", re.S)
_INLINE = re.compile(r"\$([^$]+)\$")
_BEFEHL = re.compile(r"\\([A-Za-z]+)")
_UMGEBUNG = re.compile(r"\\(begin|end)\s*\{([^{}]*)\}")


def _ohne_code(text: str) -> str:
    return _INLINE_CODE.sub(" ", _FENCE.sub(" ", text))


def _formel_fehler(formel: str) -> list[str]:
    fehler: list[str] = []
    ohne_escape = formel.replace("\\{", "").replace("\\}", "")
    tiefe = 0
    for zeichen in ohne_escape:
        if zeichen == "{":
            tiefe += 1
        elif zeichen == "}":
            tiefe -= 1
            if tiefe < 0:
                break
    if tiefe != 0:
        fehler.append(f"Geschweifte Klammern sind nicht ausgeglichen in „{formel.strip()[:40]}“.")
    # Round brackets must pair up, except where half-open intervals like [1, 2) or left/right
    # legitimately leave one open.
    if (
        formel.count("(") != formel.count(")")
        and "[" not in formel and "]" not in formel and "\\left" not in formel and "\\right" not in formel
    ):
        fehler.append(f"Runde Klammern sind nicht ausgeglichen in „{formel.strip()[:40]}“.")
    for name in dict.fromkeys(_BEFEHL.findall(formel)):
        if name not in ERLAUBTE_BEFEHLE:
            fehler.append(f"Unbekannter Formelbefehl „\\{name}“.")
    stapel: list[str] = []
    for art, name in _UMGEBUNG.findall(formel):
        if name not in _UMGEBUNGEN:
            fehler.append(f"Unzulässige Umgebung „{name}“; erlaubt sind cases und aligned.")
        elif art == "begin":
            stapel.append(name)
        elif not stapel or stapel.pop() != name:
            fehler.append(f"Umgebung „{name}“ wird geschlossen, ohne dass sie passend geöffnet wurde.")
    if stapel:
        fehler.append(f"Umgebung „{stapel[-1]}“ wird nicht geschlossen.")
    return fehler


def formel_fehler(text: str) -> list[str]:
    """Short German reasons why the formulas of `text` would not render; empty list = fine."""
    rest = _ohne_code(text).replace("\\$", " ")
    formeln: list[str] = []

    def block(treffer: re.Match[str]) -> str:
        formeln.append(treffer.group(1))
        return " "

    rest = _BLOCK.sub(block, rest)
    fehler: list[str] = []
    if "$$" in rest:
        fehler.append("Ein „$$“ wird nicht geschlossen.")
        rest = rest.replace("$$", " ")
    if rest.count("$") % 2:
        fehler.append("Ein „$“ wird nicht geschlossen; ein Dollarzeichen als Währung bitte als „\\$“ schreiben.")
    else:
        formeln += _INLINE.findall(rest)
    for formel in formeln:
        fehler += _formel_fehler(formel)
    return list(dict.fromkeys(fehler))
