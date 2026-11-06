"""Texte fuer das Board der Huelle (reine Textbausteine, kein I/O).

Die Etiketten auf dem Board kommen aus `contracts.beschriftung.beschriften`, demselben Aufruf,
der die handschriftlich gelesenen Etiketten wieder auf Antwort-IDs abbildet. So steht auf dem
Board genau das, was der Leser zurueckuebersetzt. Formeln stehen im Klartext
(`services.werkstatt.formeltext.klartext`); in keinem Text bleibt ein "$".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..contracts.beschriftung import beschriften
from ..services.werkstatt.formeltext import klartext

# Typen, bei denen die Lernenden ein Etikett (A, B, ... / 1, 2, ...) schreiben.
_MIT_ETIKETTEN = frozenset({"single_choice", "multiple_choice", "ordering", "matching"})


def _klar(text: object) -> str:
    """Klartext einer Zeile, ohne Zeilenumbruch und ohne "$"."""
    return klartext(str(text)).replace("$", "").strip()


def _zeilen(text: object) -> list[str]:
    """Mehrzeiligen Text zeilenweise in Klartext wandeln; die Umbrueche bleiben erhalten."""
    return [_klar(zeile) for zeile in str(text).replace("\r\n", "\n").split("\n")]


def _eintraege(wert: object) -> list[Mapping[str, object]]:
    return [e for e in wert if isinstance(e, Mapping)] if isinstance(wert, Sequence) and not isinstance(wert, str) else []


def _beschriftet(etiketten: Mapping[str, str], eintraege: Sequence[Mapping[str, object]]) -> list[str]:
    texte = {e.get("id"): e.get("label") for e in eintraege}
    return [f"{etikett}) {_klar(texte.get(eintrag_id) or '')}".rstrip() for etikett, eintrag_id in etiketten.items()]


def aufgabentext(task: Mapping[str, object], nummer: int) -> str:
    """„Aufgabe {nummer}: {prompt}“, bei Auswahl, Reihenfolge und Zuordnen gefolgt vom Etikettenblock."""
    typ = str(task.get("type") or "")
    zeilen = _zeilen(task.get("prompt") or "")
    zeilen[0] = f"Aufgabe {nummer}: {zeilen[0]}"
    if typ in _MIT_ETIKETTEN:
        optionen = _eintraege(task.get("options"))
        praesentation = task.get("presentation")
        praesentation = praesentation if isinstance(praesentation, dict) else {}
        etiketten = beschriften(typ, praesentation, optionen)  # type: ignore[arg-type]
        if typ == "matching":
            block = _beschriftet(etiketten.links, _eintraege(praesentation.get("left")))
            block += _beschriftet(etiketten.rechts, _eintraege(praesentation.get("right")))
        else:
            block = _beschriftet(etiketten.optionen, optionen)
        if block:
            zeilen += [""] + block
    return "\n".join(zeilen)


def rueckmeldungstext(gelesen: str | None, urteil: str, text: str) -> str:
    """Inhalt der Rueckmeldungskarte: „Gelesen: …“ (falls gesetzt), Urteil, Rueckmeldung."""
    teile: list[str] = []
    if gelesen and _klar(gelesen):
        teile.append(f"Gelesen: {_klar(gelesen)}")
    teile.append(_klar(urteil))
    absaetze = "\n".join(_zeilen(text)).strip()
    if absaetze:
        teile.append(absaetze)
    return "\n\n".join(t for t in teile if t)
