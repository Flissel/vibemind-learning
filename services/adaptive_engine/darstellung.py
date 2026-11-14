"""Fall und Vorlage einer Aufgabe: was die Lernflaeche zeigt und was die Bewerter dazu erfahren."""

from __future__ import annotations

import re

FALL_MAX = 1200
VORLAGEN: tuple[str, ...] = ("matrix", "onepager")
# Nur freie Aufgaben haben ein Canvas, auf dem eine Vorlage vorgedruckt sein kann (nicht jede Huelle druckt sie).
VORLAGE_TYPEN: frozenset[str] = frozenset({"open", "case", "penecho_canvas"})

# Feldnamen der Vorlage; eine Aufgabe darf eigene mitbringen (`vorlage_felder`), sonst gelten diese.
# Die Huelle haelt dieselben Werte (`huelle/vorlage.py`); test_vorlage_etiketten prueft die Gleichheit.
# Matrix: oben links, oben rechts, unten links, unten rechts (Nutzen nach oben, Aufwand nach rechts).
STANDARD_FELDER: dict[str, tuple[str, ...]] = {
    "matrix": ("Quick Wins", "Große Vorhaben", "Nebenbei", "Lieber nicht"),
    "onepager": ("Ausgangslage", "Empfohlene Use Cases", "Nutzen", "Aufwand", "Risiken", "Nächster Schritt"),
}
FELDER_ANZAHL: dict[str, tuple[int, int]] = {"matrix": (4, 4), "onepager": (3, 6)}
FELD_MAX = 40
_MATRIX_LAGE = ("viel Nutzen, wenig Aufwand", "viel Nutzen, viel Aufwand",
                "wenig Nutzen, wenig Aufwand", "wenig Nutzen, viel Aufwand")
_NUMMER = re.compile(r"^\s*\d+\s*[.)]\s*")

_VORGEDRUCKT = (
    "Falls vorgedruckt, zählen die vorgedruckten Beschriftungen nicht als Antwort "
    "(eine von Hand gezeichnete Vorlage zählt): "
)


def vorlage_felder(vorlage: object, felder: object) -> list[str] | None:
    """Die Feldnamen ohne Nummer und Raender, wenn sie zur Vorlage passen; sonst None (Standardnamen).

    Passend heisst: eine bekannte Vorlage, eine Liste aus Texten in der erlaubten Anzahl, jeder 1..FELD_MAX Zeichen.
    """
    if not isinstance(vorlage, str) or vorlage not in FELDER_ANZAHL or not isinstance(felder, list):
        return None
    if not all(isinstance(f, str) for f in felder):
        return None
    namen = [_NUMMER.sub("", f).strip() for f in felder]
    unten, oben = FELDER_ANZAHL[vorlage]
    if not unten <= len(namen) <= oben or not all(0 < len(n) <= FELD_MAX for n in namen):
        return None
    return namen


def _vorlage_text(vorlage: str, felder: list[str] | None) -> str:
    namen = felder or list(STANDARD_FELDER[vorlage])
    if vorlage == "matrix":
        mit_lage = ", ".join(f"{n} ({lage})" for n, lage in zip(namen, _MATRIX_LAGE))
        return _VORGEDRUCKT + f"eine Matrix Nutzen × Aufwand mit Achsen und den Feldnamen {mit_lage}."
    return _VORGEDRUCKT + f"die Gliederung {', '.join(namen)}."


def fall_und_vorlage(item_type: str, raw: dict[str, object]) -> dict[str, object]:
    """Die gueltigen Eintraege `fall`, `vorlage` und `vorlage_felder` aus der Darstellung (sonst nichts)."""
    shown: dict[str, object] = {}
    fall = raw.get("fall")
    if isinstance(fall, str) and fall.strip() and len(fall) <= FALL_MAX:
        shown["fall"] = fall
    vorlage = raw.get("vorlage")
    if item_type in VORLAGE_TYPEN and isinstance(vorlage, str) and vorlage in VORLAGEN:
        shown["vorlage"] = vorlage
        felder = vorlage_felder(vorlage, raw.get("vorlage_felder"))
        if felder is not None:
            shown["vorlage_felder"] = felder
    return shown


def bewertungshinweis(presentation: dict[str, object]) -> str | None:
    """Was ein Bewerter ueber Fall und vorgedruckte Vorlage wissen muss; None, wenn beides fehlt."""
    absaetze: list[str] = []
    fall = presentation.get("fall")
    if isinstance(fall, str) and fall:
        absaetze.append(f"Fall: {fall}")
    vorlage = presentation.get("vorlage")
    if isinstance(vorlage, str) and vorlage in STANDARD_FELDER:
        absaetze.append(_vorlage_text(vorlage, vorlage_felder(vorlage, presentation.get("vorlage_felder"))))
    return "\n\n".join(absaetze) if absaetze else None
