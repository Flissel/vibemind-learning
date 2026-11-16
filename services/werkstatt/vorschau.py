"""Preview of a task version as learners see it, plus the solution for the course lead."""

from __future__ import annotations

import hashlib
import logging
import os
import random
import re
from typing import Any

from spaces.learning.services.lernsequenz.aus_struktur import _media_available
from spaces.learning.services.lernsequenz.bilder import render_terminal
from spaces.learning.services.lernsequenz.medien import save_media
from spaces.learning.services.werkstatt.graph import graphen_zeichnen
from spaces.learning.services.werkstatt.inhalt import (
    Code,
    Fall,
    GraphAngabe,
    Luecke,
    Offen,
    Quiz,
    Reihenfolge,
    Skizze,
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


_BEZIEHUNG = {"ableitung_von": "rechts ist die Ableitung von links", "gleich": "rechts ist gleich links"}


def _graph_bilder(angaben: list[GraphAngabe]) -> list[str | None]:
    if not angaben or not _media_available():
        return [None for _ in angaben]
    try:
        return [save_media(g.png) for g in graphen_zeichnen(angaben)]
    except Exception:  # the text preview must survive a picture failure
        _LOG.warning("Vorschau-Graph konnte nicht gezeichnet werden", exc_info=True)
        return [None for _ in angaben]


def _zuordnung_mit_graphen(inhalt: Zuordnung) -> tuple[str, str, list[dict[str, Any]]]:
    """Learner text with "Graph A"/"Graph 1" in place of each graph, the drawn pictures (or
    ``None`` as a placeholder) per label, and the author's solution with the terms."""
    links_namen: list[str] = []
    buchstaben = iter(_BUCHSTABEN)
    for p in inhalt.pairs:
        links_namen.append(p.left if p.left_graph is None else f"Graph {next(buchstaben)}")
    # Shuffled by pair index; the seed sees the text or, for a graph, the term (stable per content).
    schluessel = [
        f"{i}\x00{p.right if p.right_graph is None else p.right_graph.model_dump_json()}"
        for i, p in enumerate(inhalt.pairs)
    ]
    reihe = [int(k.split("\x00", 1)[0]) for k in _gemischt(schluessel, inhalt.prompt, *(p.left for p in inhalt.pairs))]
    rechts_namen: dict[int, str] = {}
    nummern = iter(range(1, len(reihe) + 1))
    for i in reihe:
        p = inhalt.pairs[i]
        rechts_namen[i] = p.right if p.right_graph is None else f"Graph {next(nummern)}"
    links = [f"{i}. {n}" for i, n in enumerate(links_namen, start=1)]
    rechte = [f"{_BUCHSTABEN[pos]}) {rechts_namen[i]}" for pos, i in enumerate(reihe)]
    text = "\n".join([inhalt.prompt, "", *links, "", *rechte])

    gezeigt: list[tuple[str, GraphAngabe]] = [
        (links_namen[i], p.left_graph) for i, p in enumerate(inhalt.pairs) if p.left_graph is not None
    ]
    for i in reihe:
        graph = inhalt.pairs[i].right_graph
        if graph is not None:
            gezeigt.append((rechts_namen[i], graph))
    urls = _graph_bilder([g for _n, g in gezeigt])
    bilder = [{"label": n, "bild": url} for (n, _g), url in zip(gezeigt, urls)]

    def seite(text_: str, name: str, graph: GraphAngabe | None) -> str:
        return text_ if graph is None else f"{name} ({graph.term}; {text_})"

    teile = [
        f"{seite(p.left, links_namen[i], p.left_graph)} -> {seite(p.right, rechts_namen[i], p.right_graph)}"
        for i, p in enumerate(inhalt.pairs)
    ]
    loesung = "Paare: " + "; ".join(teile)
    if inhalt.beziehung is not None:
        loesung += f"; Beziehung: {_BEZIEHUNG[inhalt.beziehung]}"
    return text, loesung, bilder


_SPRACHE = {"python": "Python", "javascript": "JavaScript"}
_ERLAUBNIS = {
    "pakete": "Pakete (numpy, matplotlib)", "diagramm": "Diagramm ausgeben",
    "lange_laufzeit": "länger laufen (bis 20 Sekunden)", "daten": "Übungsdaten lesen",
}


def _erlaubnis_zeile(inhalt: Code) -> str:
    """What the learner's program may be allowed, in the fixed order of the permissions."""
    teile = []
    for kennung, wort in _ERLAUBNIS.items():
        if kennung not in inhalt.erlaubnisse:
            continue
        if kennung == "daten" and inhalt.dateien:
            wort += f" ({', '.join(d.name for d in inhalt.dateien)})"
        teile.append(wort)
    return "Der Code darf: " + ", ".join(teile)


def _testlauf_stand(inhalt: Code) -> str:
    """Where the reference solution stands: unchecked, passed, or which test cases fail."""
    from spaces.learning.services.werkstatt.pruefung import code_testlauf_codes  # pruefung imports heavy checks

    codes = code_testlauf_codes(inhalt)
    if "code_ungeprueft" in codes:
        return "noch nicht gelaufen"
    if codes:
        namen = ", ".join(inhalt.testlauf.fehlgeschlagen) if inhalt.testlauf else ""
        return f"nicht bestanden ({namen})" if namen else "nicht bestanden"
    return "bestanden"


def vorschau(content: dict[str, Any]) -> dict[str, Any]:
    inhalt = inhalt_lesen(content)
    bild: str | None = None
    extra: dict[str, Any] = {}
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
    elif isinstance(inhalt, Zuordnung) and inhalt.hat_graphen:
        text, loesung, bilder = _zuordnung_mit_graphen(inhalt)
        extra["bilder"] = bilder
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
    elif isinstance(inhalt, Skizze):
        text = inhalt.prompt
        loesung = f"Musterskizze: {inhalt.expected_answer}\nKriterien: " + "; ".join(
            f"{r.description} ({r.points})" for r in inhalt.rubric
        )
    elif isinstance(inhalt, Code):
        text = "\n".join([inhalt.prompt, "", f"Sprache: {_SPRACHE[inhalt.sprache]}",
                          *([_erlaubnis_zeile(inhalt)] if inhalt.erlaubnisse else []),
                          *(["", "Startcode:", inhalt.startcode] if inhalt.startcode.strip() else [])])
        tests = "\n".join(
            f"- {t.name}: Eingabe {t.eingabe!r} -> erwartet {t.erwartet!r}" for t in inhalt.tests
        )
        loesung = "\n".join([
            "Musterlösung:", inhalt.musterloesung, "", "Testfälle:", tests, "",
            f"Testlauf: {_testlauf_stand(inhalt)}",
            "Kriterien: " + "; ".join(f"{r.description} ({r.points})" for r in inhalt.rubric),
        ])
    else:  # pragma: no cover - the union is closed
        raise TypeError(type(inhalt).__name__)
    lektion = f"{inhalt.lektion.title}\n\n{inhalt.lektion.body}" if inhalt.lektion else None
    return {"text": text, "bild": bild, "loesung": loesung, "lektion": lektion, **extra}
