"""Preview of a task version as learners see it, plus the solution for the course lead."""

from __future__ import annotations

import hashlib
import logging
import os
import random
import re
from typing import Any

from spaces.learning.services.lernsequenz.bilder import render_terminal
from spaces.learning.services.lernsequenz.medien import save_media
from spaces.learning.services.werkstatt.inhalt import (
    Fall,
    Luecke,
    Offen,
    Quiz,
    Reihenfolge,
    Zahl,
    Zuordnung,
    inhalt_lesen,
)

_LOG = logging.getLogger(__name__)
_BUCHSTABEN = "ABCDEF"
_PLATZHALTER = re.compile(r"\{\{([^{}]+)\}\}")


def _gemischt(werte: list[str], *teile: str) -> list[str]:
    """Shuffled with a seed from the content only: the same content always shows the same order.

    Never the solution order itself (for more than one distinct value): the preview must not
    show the answer as the question."""
    if len(set(werte)) < 2:
        return list(werte)
    for zaehler in range(32):
        saat = hashlib.sha256("".join((*teile, str(zaehler), *werte)).encode()).digest()
        ergebnis = list(werte)
        random.Random(int.from_bytes(saat[:8], "big")).shuffle(ergebnis)
        if ergebnis != werte:
            return ergebnis
    return werte[1:] + werte[:1]


def _bild(inhalt: Luecke) -> str | None:
    if inhalt.shell is None or not os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip():
        return None
    antworten = {b.blank_id: b.accepted[0] for b in inhalt.blanks}
    try:
        fenster = render_terminal(inhalt.shell, inhalt.template, blanks=antworten)
        return save_media(fenster.png)
    except Exception:  # the text preview must survive a picture failure
        _LOG.warning("Vorschau-Bild konnte nicht erzeugt werden", exc_info=True)
        return None


def vorschau(content: dict[str, Any]) -> dict[str, Any]:
    inhalt = inhalt_lesen(content)
    bild: str | None = None
    if isinstance(inhalt, Quiz):
        zeilen = [f"{_BUCHSTABEN[i]}) {o.text}" for i, o in enumerate(inhalt.options)]
        text = "\n".join([inhalt.prompt, "", *zeilen])
        richtig = [
            _BUCHSTABEN[i] for i, o in enumerate(inhalt.options) if o.option_id in inhalt.correct_option_ids
        ]
        loesung = "Richtig: " + ", ".join(richtig)
    elif isinstance(inhalt, (Offen, Fall)):
        text = inhalt.prompt
        loesung = f"Erwartete Antwort: {inhalt.expected_answer}"
    elif isinstance(inhalt, Luecke):
        text = f"{inhalt.prompt}\n\n{_PLATZHALTER.sub('____', inhalt.template)}"
        bild = _bild(inhalt)
        loesung = "Akzeptiert: " + "; ".join(
            f"{b.blank_id}: {' / '.join(b.accepted)}" for b in inhalt.blanks
        )
    elif isinstance(inhalt, Reihenfolge):
        gemischt = _gemischt(inhalt.items, inhalt.prompt)
        text = "\n".join([inhalt.prompt, "", *(f"{i}. {s}" for i, s in enumerate(gemischt, start=1))])
        loesung = "Richtige Reihenfolge: " + " > ".join(inhalt.items)
    elif isinstance(inhalt, Zuordnung):
        rechts = _gemischt([p.right for p in inhalt.pairs], inhalt.prompt, *(p.left for p in inhalt.pairs))
        links = [f"{i}. {p.left}" for i, p in enumerate(inhalt.pairs, start=1)]
        rechte = [f"{_BUCHSTABEN[i]}) {r}" for i, r in enumerate(rechts)]
        text = "\n".join([inhalt.prompt, "", *links, "", *rechte])
        loesung = "Paare: " + "; ".join(f"{p.left} -> {p.right}" for p in inhalt.pairs)
    elif isinstance(inhalt, Zahl):
        feld = f"{inhalt.label} ____" if inhalt.label else "____"
        text = "\n".join([inhalt.prompt, "", feld])
        loesung = f"Lösung: {inhalt.value:g} (± {inhalt.tolerance:g}); Rechnung: {inhalt.rechnung}"
    else:  # pragma: no cover - the union is closed
        raise TypeError(type(inhalt).__name__)
    lektion = f"{inhalt.lektion.title}\n\n{inhalt.lektion.body}" if inhalt.lektion else None
    return {"text": text, "bild": bild, "loesung": loesung, "lektion": lektion}
