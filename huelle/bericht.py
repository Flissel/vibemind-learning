"""Bericht am Kursende: Text der Berichtskarte auf dem Board (Markdown).

Zaehlt nur, was richtig ist. Die Zahl der Anlaeufe kommt hier nicht vor (Spec, Entscheidung 5).
"""

from __future__ import annotations

import re
from typing import Any

# Dieselben Grenzen wie Learnings `_turn_feedback` (services/adaptive_engine/session_service.py).
GRENZE_RICHTIG = 0.999
GRENZE_TEILWEISE = 0.65


def urteil(score: float) -> str:
    if score >= GRENZE_RICHTIG:
        return "richtig"
    if score >= GRENZE_TEILWEISE:
        return "teilweise"
    return "falsch"


_MARKDOWN = re.compile(r"([\\`*_\[\]<>#|~])")


def rein(text: str) -> str:
    """Fremden Text fuer Markdown entschaerfen: Zeilenumbrueche zu Leerzeichen, Formatzeichen maskiert."""
    return _MARKDOWN.sub(r"\\\1", " ".join(str(text).split()))


def _zahl(wert: Any) -> float | None:
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        return None
    return float(wert)


def aufgaben(summary: dict[str, Any]) -> list[tuple[int, float]]:
    """(Nummer, Score) je Ergebnis in Reihenfolge; ohne gueltigen Score faellt der Eintrag weg."""
    ergebnis = []
    roh = summary.get("results")
    for position, eintrag in enumerate(roh if isinstance(roh, list) else [], start=1):
        if not isinstance(eintrag, dict):
            continue
        score = _zahl(eintrag.get("score"))
        if score is None:
            continue
        nummer = eintrag.get("ordinal")
        ergebnis.append((nummer if isinstance(nummer, int) and not isinstance(nummer, bool) else position, score))
    return ergebnis


_ABSCHLUSS = "Kurs abgeschlossen: "
_ABSCHLUSS_ZEILE = 64  # Zeichen je Zeile im 740 breiten Platz (36 je 400 px, vorsichtig)


def abschluss_text(kurs_titel: str, summary: dict[str, Any]) -> str:
    """Kurze Abschlusskarte der Gruppe (Final B, H3): Kurs und Zahl der richtigen Aufgaben, Klartext ohne Markdown.

    Sie steht als Text im Kopf der Mappe (110 hoch); hoechstens zwei Zeilen, der Titel wird bei Bedarf gekuerzt.
    """
    titel = " ".join(str(kurs_titel).split())
    platz = _ABSCHLUSS_ZEILE - len(_ABSCHLUSS)
    if len(titel) > platz:
        titel = titel[: platz - 1].rstrip() + "…"
    zeilen = [_ABSCHLUSS + titel]
    einzeln = aufgaben(summary)
    if einzeln:
        richtig = sum(1 for _, score in einzeln if urteil(score) == "richtig")
        zeilen.append(f"{richtig} von {len(einzeln)} Aufgaben richtig.")
    return "\n".join(zeilen)


def bericht_text(kurs_titel: str, summary: dict[str, Any], begriffe: list[dict[str, Any]]) -> str:
    zeilen = [f"# Bericht: {rein(kurs_titel)}", ""]
    einzeln = aufgaben(summary)
    if einzeln:
        # Kopfzeile und Liste zaehlen nach denselben Grenzen (nicht nach Learnings `correct`, das ab 0,65 zaehlt).
        richtig = sum(1 for _, score in einzeln if urteil(score) == "richtig")
        zeilen += [f"**{richtig} von {len(einzeln)} richtig**", ""]
        zeilen += [f"- Aufgabe {nummer}: {urteil(score)}" for nummer, score in einzeln]
        zeilen.append("")
    gelistet = []
    for begriff in begriffe if isinstance(begriffe, list) else []:
        if not isinstance(begriff, dict):
            continue
        titel = begriff.get("titel")
        stand = _zahl(begriff.get("beherrschung"))
        if isinstance(titel, str) and titel and stand is not None:
            gelistet.append(f"- {rein(titel)}: {round(min(max(stand, 0.0), 1.0) * 100)} %")
    if gelistet:
        zeilen += ["**Begriffe**", "", *gelistet]
    return "\n".join(zeilen).rstrip() + "\n"
