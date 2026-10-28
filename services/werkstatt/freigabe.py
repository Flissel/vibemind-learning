"""Freigabe der Werkstatt: Schluessel, Manifest, Vergleich mit der letzten Freigabe, Kursregeln.

Nothing here calls a model. `freigabe_vorbereiten` only reads the Werkstatt and writes one
`angefordert` row; publishing is a separate, deliberate step.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from typing import Any, Protocol

from pydantic import ValidationError

from spaces.learning.services.course_factory.learnhouse_gateway import LearnHouseGatewayError
from spaces.learning.services.course_factory.quality_gate import (
    AktivitaetsSicht,
    LektionsSicht,
    kurs_regeln,
)
from spaces.learning.services.werkstatt.inhalt import Fall, Offen, Quiz, _loesung, inhalt_lesen
from spaces.learning.services.werkstatt.pruefung import _TEXTE, _als_aktivitaet, _als_lektion
from spaces.learning.services.werkstatt.store import WerkstattStore

logger = logging.getLogger(__name__)

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


class _StandSchreiber(Protocol):
    async def werkstatt_stand_schreiben(
        self, course_id: str, *, expected_revision: int, freigabe_id: str,
        kapitel: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class _Katalog(Protocol):
    def letzte_revision(self, course_id: str, *, unter: int) -> int | None: ...

    def hat_revision(self, course_id: str, revision: int) -> bool: ...

    def projizieren(
        self, course_id: str, course_revision: int, eintraege: list[dict[str, Any]],
        vorher_revision: int | None, vorher_eintraege: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


def _fassungs_inhalt(store: WerkstattStore, task_id: str, number: int) -> dict[str, Any]:
    fassung = next((f for f in store.fassungen(task_id) if f["number"] == number), None)
    if fassung is None:
        raise ValueError("manifest_fassung_fehlt")
    return fassung["content"]


def _kapitel_aus_manifest(store: WerkstattStore, manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chapters in manifest order; each task's lesson first. Reads the approved version, not the newest."""
    kapitel: dict[str, dict[str, Any]] = {}
    for m in sorted(manifest, key=lambda e: e["position"]):
        inhalt = inhalt_lesen(_fassungs_inhalt(store, m["task_id"], m["fassung"]))
        eintraege = kapitel.setdefault(
            m["kapitel"], {"titel": m["kapitel"], "eintraege": []}
        )["eintraege"]
        if inhalt.lektion is not None:
            eintraege.append({
                "key": m["lektion_key"], "art": "lesson", "titel": inhalt.lektion.title,
                "markdown": inhalt.lektion.body,
                "claims": [c.text for c in inhalt.lektion.claims],
            })
        eintraege.append({
            "key": m["aufgaben_key"], "art": "task", "titel": inhalt.prompt[:120],
            "prompt": inhalt.prompt,
        })
    return list(kapitel.values())


def _mit_inhalt(store: WerkstattStore, manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**m, "content": _fassungs_inhalt(store, m["task_id"], m["fassung"])} for m in manifest]


def _uebernommen(store: WerkstattStore, course_id: str) -> list[dict[str, Any]]:
    """Before the first release: the taken-over Fassungen, i.e. what the factory catalog held."""
    eintraege = []
    for a in store.aufgaben_alle(course_id):
        f = next((f for f in store.fassungen(a["task_id"]) if f["art"] == "uebernommen"), None)
        if f is not None:
            eintraege.append({
                "task_id": a["task_id"], "fassung": f["number"],
                "loesung_hash": loesung_hash(f["content"]), "content": f["content"],
            })
    return eintraege


def _katalog_projizieren(
    store: WerkstattStore, katalog: _Katalog, freigabe: dict[str, Any], revision: int
) -> dict[str, Any]:
    """Catalog of the new LearnHouse revision; learner state from the newest catalog below it.

    That catalog belongs to the earlier release with its revision (a release whose projection
    failed has none and is skipped); with no such release it is the taken-over course's
    catalog, compared with the taken-over Fassungen."""
    course_id = freigabe["course_id"]
    vorher_revision = katalog.letzte_revision(course_id, unter=revision)
    vorher: list[dict[str, Any]] = []
    if vorher_revision is not None:
        passend = next(
            (f for f in store.freigaben_freigegeben(course_id)
             if f["nummer"] < freigabe["nummer"] and f["learnhouse_revision"] == vorher_revision),
            None,
        )
        vorher = _mit_inhalt(store, passend["manifest"]) if passend else _uebernommen(store, course_id)
    return katalog.projizieren(
        course_id, revision, _mit_inhalt(store, freigabe["manifest"]), vorher_revision,
        vorher_eintraege=vorher,
    )


def katalog_nachholen(store: WerkstattStore, katalog: _Katalog, course_id: str) -> dict[str, Any]:
    """Project the newest released revision that has no catalog (its projection failed).

    Same carry-over as at release time; `{"nachgeholt": False}` when nothing is missing."""
    fehlend = next(
        (f for f in store.freigaben_freigegeben(course_id)
         if f["learnhouse_revision"] is not None
         and not katalog.hat_revision(course_id, f["learnhouse_revision"])),
        None,
    )
    if fehlend is None:
        return {"nachgeholt": False}
    revision = fehlend["learnhouse_revision"]
    try:
        ergebnis = _katalog_projizieren(store, katalog, fehlend, revision)
    except Exception:
        logger.exception(
            "werkstatt_katalog_nachholen_fehlgeschlagen",
            extra={"freigabe_id": fehlend["id"], "course_revision": revision},
        )
        raise
    logger.info(
        "werkstatt_katalog_nachgeholt",
        extra={"freigabe_id": fehlend["id"], "course_revision": revision, **ergebnis},
    )
    return {"freigabe_id": fehlend["id"], "learnhouse_revision": revision, "katalog": ergebnis}


async def freigabe_ausfuehren(
    store: WerkstattStore, gateway: _StandSchreiber, freigabe_id: str, *,
    approval_ref: str, learnhouse_revision: int, katalog: _Katalog | None = None,
) -> dict[str, Any]:
    """Write the approved manifest into LearnHouse. All checks run before the first write.

    On a gateway error the release stays `angefordert`. With `katalog`, the practice catalog of
    the new revision is projected afterwards; a catalog failure is logged and reported as
    `katalog_fehler`, it never undoes the release. No model is called."""
    freigabe = store.freigabe(freigabe_id)
    if freigabe["zustand"] != "angefordert":
        raise ValueError("freigabe_nicht_offen")
    if freigabe["learnhouse_revision_vorher"] != learnhouse_revision:
        raise ValueError("learnhouse_stand_veraltet")
    if any(b.get("blockierend") for b in freigabe["befunde"]):
        raise ValueError("freigabe_blockiert")
    vergleich = freigabe["vergleich"]
    if not any(vergleich.get(k) for k in ("neu", "geaendert", "textkorrektur", "geloescht")):
        raise ValueError("nichts_zu_veroeffentlichen")
    kapitel = _kapitel_aus_manifest(store, freigabe["manifest"])
    try:
        antwort = await gateway.werkstatt_stand_schreiben(
            freigabe["course_id"], expected_revision=learnhouse_revision, freigabe_id=freigabe_id,
            kapitel=kapitel,
        )
    except LearnHouseGatewayError as fehler:
        if fehler.status == 409 and fehler.code == "revision_conflict":
            raise ValueError("learnhouse_stand_veraltet") from fehler
        if fehler.status == 409 and fehler.code == "freigabe_id_reused":
            raise ValueError("freigabe_id_reused") from fehler
        raise
    revision = int(antwort["revision"])
    store.freigabe_markieren_freigegeben(
        freigabe_id, learnhouse_revision=revision, approval_ref=approval_ref
    )
    ergebnis: dict[str, Any] = {
        "learnhouse_revision": revision, "aktualisiert": antwort["aktualisiert"],
        "neu": antwort["neu"], "unveroeffentlicht": antwort["unveroeffentlicht"],
    }
    if katalog is not None:
        # After the release is recorded: a catalog failure never hides it. The projection is
        # idempotent and can be repeated for this revision.
        try:
            ergebnis["katalog"] = _katalog_projizieren(store, katalog, freigabe, revision)
        except Exception as fehler:
            logger.exception(
                "werkstatt_katalog_projektion_fehlgeschlagen",
                extra={"freigabe_id": freigabe_id, "course_revision": revision},
            )
            ergebnis["katalog_fehler"] = type(fehler).__name__
    return ergebnis
