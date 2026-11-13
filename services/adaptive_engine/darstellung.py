"""Fall und Vorlage einer Aufgabe: was die Lernflaeche zeigt und was die Bewerter dazu erfahren."""

from __future__ import annotations

FALL_MAX = 1200
VORLAGEN: tuple[str, ...] = ("matrix", "onepager")
# Nur freie Aufgaben haben ein Canvas, auf dem eine Vorlage vorgedruckt sein kann.
VORLAGE_TYPEN: frozenset[str] = frozenset({"open", "case", "penecho_canvas"})

_VORGEDRUCKT = "Auf dem Canvas vorgedruckt (nicht Teil der Antwort): "
_VORLAGE_TEXT: dict[str, str] = {
    "matrix": (
        _VORGEDRUCKT
        + "eine Matrix Nutzen × Aufwand mit Achsen und den Feldnamen "
        "Quick Wins, Große Vorhaben, Nebenbei, Lieber nicht."
    ),
    "onepager": (
        _VORGEDRUCKT
        + "die Gliederung Ausgangslage, Empfohlene Use Cases, Nutzen, Aufwand, Risiken, Nächster Schritt."
    ),
}


def fall_und_vorlage(item_type: str, raw: dict[str, object]) -> dict[str, object]:
    """Die gueltigen Eintraege `fall` und `vorlage` aus der Darstellung (sonst nichts)."""
    shown: dict[str, object] = {}
    fall = raw.get("fall")
    if isinstance(fall, str) and fall.strip() and len(fall) <= FALL_MAX:
        shown["fall"] = fall
    vorlage = raw.get("vorlage")
    if item_type in VORLAGE_TYPEN and isinstance(vorlage, str) and vorlage in VORLAGEN:
        shown["vorlage"] = vorlage
    return shown


def bewertungshinweis(presentation: dict[str, object]) -> str | None:
    """Was ein Bewerter ueber Fall und vorgedruckte Vorlage wissen muss; None, wenn beides fehlt."""
    absaetze: list[str] = []
    fall = presentation.get("fall")
    if isinstance(fall, str) and fall:
        absaetze.append(f"Fall: {fall}")
    vorlage = presentation.get("vorlage")
    if isinstance(vorlage, str) and vorlage in _VORLAGE_TEXT:
        absaetze.append(_VORLAGE_TEXT[vorlage])
    return "\n\n".join(absaetze) if absaetze else None
