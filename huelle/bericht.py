"""Bericht am Kursende: Text der Berichtskarte auf dem Board (Markdown).

Zaehlt nur, was richtig ist. Die Zahl der Anlaeufe kommt hier nicht vor (Spec, Entscheidung 5).
"""

from __future__ import annotations

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


def bericht_text(kurs_titel: str, summary: dict[str, Any], begriffe: list[dict[str, Any]]) -> str:
    zeilen = [f"# Bericht: {kurs_titel}", ""]
    richtig, bearbeitet = summary.get("correct"), summary.get("attempted")
    if isinstance(richtig, int) and isinstance(bearbeitet, int):
        zeilen += [f"**{richtig} von {bearbeitet} richtig**", ""]
    einzeln = aufgaben(summary)
    if einzeln:
        zeilen += [f"- Aufgabe {nummer}: {urteil(score)}" for nummer, score in einzeln]
        zeilen.append("")
    gelistet = []
    for begriff in begriffe if isinstance(begriffe, list) else []:
        if not isinstance(begriff, dict):
            continue
        titel = begriff.get("titel")
        stand = _zahl(begriff.get("beherrschung"))
        if isinstance(titel, str) and titel and stand is not None:
            gelistet.append(f"- {titel}: {round(min(max(stand, 0.0), 1.0) * 100)} %")
    if gelistet:
        zeilen += ["**Begriffe**", "", *gelistet]
    return "\n".join(zeilen).rstrip() + "\n"
