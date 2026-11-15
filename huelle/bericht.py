"""Bericht am Kursende: Text der Berichtskarte auf dem Board (Markdown).

Zaehlt die Urteile der Aufgaben. Es zaehlt der beste Versuch; wie viele Versuche es waren und wie der erste
ausging, steht daneben (Qualitaetsrunde 15.11., loest Entscheidung 5 der frueheren Spec ab).
"""

from __future__ import annotations

import re
from typing import Any

# Das Urteil je Aufgabe liefert Learning mit (`summary.results[].state`, Regel in services/evaluation/urteil.py).
# Diese Grenzen gelten nur noch fuer Antworten ohne `state` (aeltere Learning-Fassung).
GRENZE_RICHTIG = 0.999
GRENZE_TEILWEISE = 0.65
_URTEIL_AUS_STATE = {"correct": "richtig", "partial": "teilweise", "incorrect": "falsch"}


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


def urteile(summary: dict[str, Any]) -> list[tuple[int, str]]:
    """(Nummer, „richtig“ | „teilweise“ | „falsch“) je Ergebnis: Learnings `state`, sonst nach dem Score."""
    ergebnis = []
    roh = summary.get("results")
    for position, eintrag in enumerate(roh if isinstance(roh, list) else [], start=1):
        if not isinstance(eintrag, dict):
            continue
        score = _zahl(eintrag.get("score"))
        if score is None:
            continue
        nummer = eintrag.get("ordinal")
        state = eintrag.get("state")
        wort = _URTEIL_AUS_STATE.get(state) if isinstance(state, str) else None
        ergebnis.append((nummer if isinstance(nummer, int) and not isinstance(nummer, bool) else position,
                         wort or urteil(score)))
    return ergebnis


def zaehlung(einzeln: list[tuple[int, str]], *, was: str = "") -> str:
    """„4 von 6 richtig, 1 teilweise“ (mit `was` „4 von 6 Aufgaben richtig, …“); „teilweise“ nur, wenn es vorkommt."""
    richtig = sum(1 for _, wort in einzeln if wort == "richtig")
    teilweise = sum(1 for _, wort in einzeln if wort == "teilweise")
    text = f"{richtig} von {len(einzeln)} {was + ' ' if was else ''}richtig"
    return f"{text}, {teilweise} teilweise" if teilweise else text


def versuche_text(versuche: int, erstes_urteil: str | None, endurteil: str | None = None) -> str:
    """„3 Versuche, zuerst falsch“ neben dem Ergebnis; bei hoechstens einem Versuch leer.

    `erstes_urteil` ist Learnings Zustand des ersten bewerteten Versuchs; „zuerst …“ entfaellt, wenn er
    unbekannt ist oder dem Endurteil gleicht."""
    if isinstance(versuche, bool) or not isinstance(versuche, int) or versuche < 2:
        return ""
    zuerst = _URTEIL_AUS_STATE.get(erstes_urteil) if isinstance(erstes_urteil, str) else None
    return f"{versuche} Versuche" + (f", zuerst {zuerst}" if zuerst and zuerst != endurteil else "")


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
    einzeln = urteile(summary)
    if einzeln:
        zeilen.append(zaehlung(einzeln, was="Aufgaben") + ".")
    return "\n".join(zeilen)


def bericht_text(kurs_titel: str, summary: dict[str, Any], begriffe: list[dict[str, Any]],
                 versuche: dict[int, tuple[int, str | None]] | None = None) -> str:
    """`versuche` (optional): je Aufgabennummer (Zahl der Versuche, Zustand des ersten)."""
    zeilen = [f"# Bericht: {rein(kurs_titel)}", ""]
    einzeln = urteile(summary)
    if einzeln:
        # Kopfzeile und Liste zaehlen dieselben Urteile.
        zeilen += [f"**{zaehlung(einzeln)}**", ""]
        for nummer, wort in einzeln:
            zusatz = versuche_text(*(versuche or {}).get(nummer, (0, None)), wort)
            zeilen.append(f"- Aufgabe {nummer}: {wort}" + (f" ({zusatz})" if zusatz else ""))
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
