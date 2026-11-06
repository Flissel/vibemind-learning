"""Texte fuer das Board der Huelle (reine Textbausteine, kein I/O).

Die Etiketten auf dem Board kommen aus `contracts.beschriftung.beschriften`, demselben Aufruf,
der die handschriftlich gelesenen Etiketten wieder auf Antwort-IDs abbildet. So steht auf dem
Board genau das, was der Leser zurueckuebersetzt. Formeln bleiben als LaTeX (`$...$`, `$$...$$`)
im Text, PenEcho setzt sie (mixed-text.js / note-card.js). Nur wenn ein Text unausgewogene oder
preisartige "$" enthaelt, wandelt `formel_oder_klartext` ihn in Klartext
(`services.werkstatt.formeltext.klartext`), dann bleibt kein "$" darin.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from ..contracts.beschriftung import beschriften
from ..services.werkstatt.formeltext import klartext

# Typen, bei denen die Lernenden ein Etikett (A, B, ... / 1, 2, ...) schreiben.
_MIT_ETIKETTEN = frozenset({"single_choice", "multiple_choice", "ordering", "matching"})

# Formeln, die PenEcho sicher setzt: `$$..$$` (nur Board) und `$..$` ohne Leerraum an den Raendern,
# nicht gefolgt von einer Ziffer (note-card.js verlangt das, sonst waere "5 $ und 6 $" Mathe).
_ANZEIGE = re.compile(r"\$\$[^$]+?\$\$")
_INLINE = re.compile(r"\$(?!\s)[^$\n]*?[^$\s\\]\$(?!\d)|\$[^$\s\\]\$(?!\d)")


def formel_oder_klartext(text: object, *, anzeige: bool = True) -> str:
    """Zeile mit LaTeX-Formeln oder, wenn ein "$" nicht sicher Mathe ist, deren Klartext.

    LaTeX bleibt nur, wenn mindestens eine Formel da ist, kein `\\$` vorkommt und nach dem
    Entfernen aller Formeln kein "$" uebrig bleibt. `anzeige=False` (Notizkarten setzen `$$`
    nur am Zeilenanfang) lehnt `$$` ab. Sonst gilt `klartext` ohne "$"; Leerraum wird gekuerzt.
    """
    roh = str(text)
    rest = roh if not anzeige else _ANZEIGE.sub("", roh)
    ohne = _INLINE.sub("", rest)
    if "$" in roh and "\\$" not in roh and "$" not in ohne and (anzeige or "$$" not in roh):
        return " ".join(roh.split())
    return klartext(roh).replace("$", "").strip()


def _klar(text: object) -> str:
    """Klartext einer Zeile, ohne Zeilenumbruch und ohne "$"."""
    return klartext(str(text)).replace("$", "").strip()


def _zeilen(text: object, *, anzeige: bool = True) -> list[str]:
    """Mehrzeiligen Text zeilenweise bearbeiten (Formeln bleiben, sonst Klartext); Umbrueche bleiben."""
    return [formel_oder_klartext(zeile, anzeige=anzeige) for zeile in str(text).replace("\r\n", "\n").split("\n")]


def _eintraege(wert: object) -> list[Mapping[str, object]]:
    return [e for e in wert if isinstance(e, Mapping)] if isinstance(wert, Sequence) and not isinstance(wert, str) else []


def _beschriftet(etiketten: Mapping[str, str], eintraege: Sequence[Mapping[str, object]]) -> list[str]:
    texte = {e.get("id"): e.get("label") for e in eintraege}
    return [f"{etikett}) {formel_oder_klartext(texte.get(eintrag_id) or '')}".rstrip() for etikett, eintrag_id in etiketten.items()]


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
    absaetze = "\n".join(_zeilen(text, anzeige=False)).strip()
    if absaetze:
        teile.append(absaetze)
    return "\n\n".join(t for t in teile if t)
