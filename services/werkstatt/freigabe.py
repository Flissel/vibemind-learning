"""Freigabe der Werkstatt: Schluessel, Manifest, Vergleich mit der letzten Freigabe, Kursregeln.

Nothing here calls a model. `freigabe_vorbereiten` only reads the Werkstatt and writes one
`angefordert` row; publishing is a separate, deliberate step.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from pydantic import ValidationError

from spaces.learning.services.course_factory.quality_gate import (
    AktivitaetsSicht,
    LektionsSicht,
    kurs_regeln,
)
from spaces.learning.services.werkstatt.inhalt import Fall, Offen, Quiz, _loesung, inhalt_lesen
from spaces.learning.services.werkstatt.pruefung import _TEXTE, _als_aktivitaet, _als_lektion
from spaces.learning.services.werkstatt.store import WerkstattStore

# Findings that remind but do not hold a release back.
_NICHT_BLOCKIEREND = frozenset({"rubric_scale_inconsistent", "lesson_body_thin"})

# In the Werkstatt only a mixed form of address holds a release back; the other course-wide
# rules are hints (the factory's own gate is unchanged).
_KURS_SPERREN = frozenset({"address_mixed"})


def _kurs_text(code: str, subject: str, sichten: list[AktivitaetsSicht]) -> str:
    if code == "concept_missing_lesson":
        return f"Zum Konzept „{subject}“ gibt es im Kurs keine Lektion, die es vermittelt."
    if code == "concept_missing_activity":
        return f"Zum Konzept „{subject}“ gibt es im Kurs keine Aufgabe, die es übt."
    if code == "task_type_distribution":
        zaehler = Counter(a.typ for a in sichten)
        typ, anzahl = zaehler.most_common(1)[0]
        anteil = round(100 * anzahl / len(sichten))
        return (
            f"Die Aufgabentypen sind zu einseitig verteilt: {anzahl} von {len(sichten)} Aufgaben "
            f"({anteil} %) sind vom Typ „{typ}“. Mehr Abwechslung hilft den Lernenden."
        )
    if code == "address_mixed":
        return _TEXTE["address_mixed"]
    if code == "rubric_scale_inconsistent":
        return "Die Bewertungskriterien nutzen unterschiedliche Punkteskalen."
    return "Die Kursprüfung hat ein Problem gefunden."


def werkstatt_schluessel(aufgabe: dict[str, Any]) -> tuple[str, str]:
    """`(aufgaben_key, lektion_key)`: die Herkunft aus der Fabrik, sonst ein Werkstatt-Schluessel."""
    task_id = aufgabe["task_id"]
    return (
        aufgabe.get("herkunft_key") or f"werkstatt:{task_id}",
        aufgabe.get("lektion_herkunft_key") or f"werkstatt:{task_id}:lektion",
    )


def loesung_hash(content: dict[str, Any]) -> str:
    """sha256 over what `fassungs_art` calls 'neu': kind, concepts and the solution (order-free)."""
    inhalt = inhalt_lesen(content)
    roh = json.dumps(
        [inhalt.kind, sorted(inhalt.concept_ids), _loesung(inhalt)],
        sort_keys=True, ensure_ascii=False, default=list,
    )
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()


def inhalt_hash(content: dict[str, Any]) -> str:
    """sha256 over the whole canonical content: equal means the very same task text and solution."""
    roh = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()


def _eintrag(aufgabe: dict[str, Any], fassung_alt: int | None, fassung_neu: int | None) -> dict[str, Any]:
    return {
        "task_id": aufgabe["task_id"], "kapitel": aufgabe["chapter_title"],
        "summary": aufgabe["summary"], "fassung_alt": fassung_alt, "fassung_neu": fassung_neu,
    }


def _vergleichen(
    manifest: list[dict[str, Any]], alt: list[dict[str, Any]], aufgaben: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    vorher = {m["task_id"]: m for m in alt}
    jetzt = {m["task_id"] for m in manifest}
    vergleich: dict[str, Any] = {
        "neu": [], "geaendert": [], "textkorrektur": [], "geloescht": [], "unveraendert": 0,
    }
    for m in manifest:
        a = aufgaben[m["task_id"]]
        v = vorher.get(m["task_id"])
        if v is None:
            vergleich["neu"].append(_eintrag(a, None, m["fassung"]))
        elif v["fassung"] == m["fassung"] or v.get("inhalt_hash") == m["inhalt_hash"]:
            # Also a task brought back to the released content (new number, same content).
            vergleich["unveraendert"] += 1
        elif v["loesung_hash"] == m["loesung_hash"]:
            vergleich["textkorrektur"].append(_eintrag(a, v["fassung"], m["fassung"]))
        else:
            vergleich["geaendert"].append(_eintrag(a, v["fassung"], m["fassung"]))
    for v in alt:
        if v["task_id"] not in jetzt:
            a = aufgaben.get(v["task_id"]) or {
                "task_id": v["task_id"], "chapter_title": v["kapitel"], "summary": "",
            }
            vergleich["geloescht"].append(_eintrag(a, v["fassung"], None))
    return vergleich


def freigabe_vorbereiten(
    store: WerkstattStore, course_id: str, *, autor: str, learnhouse_revision: int
) -> dict[str, Any]:
    kurs = store.kurs(course_id)
    aufgaben = {a["task_id"]: a for a in store.aufgaben_alle(course_id)}
    befunde: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    sichten: list[AktivitaetsSicht] = []
    lektionen: list[LektionsSicht] = []
    for a in kurs["aufgaben"]:
        neueste = store.neueste(a["task_id"])
        if neueste is None:
            befunde.append({
                "code": "aufgabe_ohne_fassung", "task_id": a["task_id"], "blockierend": True,
                "text": f"Die Aufgabe „{a['summary']}“ hat noch keinen Inhalt und kann nicht veröffentlicht werden.",
            })
            continue
        try:
            inhalt = inhalt_lesen(neueste["content"])
            hash_ = loesung_hash(neueste["content"])
        except ValidationError:
            befunde.append({
                "code": "aufgabe_mit_befunden", "task_id": a["task_id"], "blockierend": True,
                "text": f"Der Inhalt der Aufgabe „{a['summary']}“ ist ungültig und kann nicht veröffentlicht werden.",
            })
            continue
        aufgaben_key, lektion_key = werkstatt_schluessel(a)
        manifest.append({
            "task_id": a["task_id"], "fassung": neueste["number"], "loesung_hash": hash_,
            "inhalt_hash": inhalt_hash(neueste["content"]),
            "kapitel": a["chapter_title"], "position": a["position"],
            "aufgaben_key": aufgaben_key, "lektion_key": lektion_key,
        })
        blockierend = [f for f in neueste["findings"] if f.get("code") not in _NICHT_BLOCKIEREND]
        if blockierend:
            texte = " ".join(str(f.get("text") or f.get("code", "")) for f in blockierend)
            befunde.append({
                "code": "aufgabe_mit_befunden", "task_id": a["task_id"], "blockierend": True,
                "text": f"Die Aufgabe „{a['summary']}“ hat offene Befunde: {texte}",
            })
        punkte = None
        if isinstance(inhalt, (Quiz, Offen, Fall)):
            rubrik = _als_aktivitaet(inhalt).rubric
            punkte = sum(c.points for c in rubrik) if rubrik else None
        sichten.append(
            AktivitaetsSicht(a["task_id"], inhalt.kind, tuple(inhalt.concept_ids), inhalt.prompt, punkte)
        )
        if inhalt.lektion is not None:
            lek = _als_lektion(inhalt, inhalt.lektion)
            lektionen.append(LektionsSicht(a["task_id"], tuple(lek.concept_ids), lek.body))
    if not kurs["aufgaben"]:
        befunde.append({
            "code": "kurs_ohne_aufgaben", "task_id": None, "blockierend": True,
            "text": "Der Kurs hat noch keine Aufgaben, es gibt nichts zu veröffentlichen.",
        })
    for issue in kurs_regeln(sichten, lektionen, kurs_id=course_id):
        befunde.append({
            "code": issue.code, "task_id": None, "blockierend": issue.code in _KURS_SPERREN,
            "text": _kurs_text(issue.code, issue.subject_id, sichten),
        })
    letzte = store.freigabe_letzte_freigegebene(course_id)
    vergleich = _vergleichen(manifest, letzte["manifest"] if letzte else [], aufgaben)
    blockiert = any(b["blockierend"] for b in befunde)
    nichts = not any(vergleich[k] for k in ("neu", "geaendert", "textkorrektur", "geloescht"))
    angelegt = store.freigabe_anlegen(
        course_id, manifest=manifest, vergleich=vergleich, befunde=befunde,
        learnhouse_revision_vorher=learnhouse_revision, autor=autor,
    )
    return {
        "freigabe_id": angelegt["id"], "nummer": angelegt["nummer"], "vergleich": vergleich,
        "befunde": befunde, "blockiert": blockiert, "nichts_zu_veroeffentlichen": nichts,
    }
