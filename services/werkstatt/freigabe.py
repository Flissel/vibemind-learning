"""Freigabe der Werkstatt: Schluessel, Manifest, Vergleich mit der letzten Freigabe, Kursregeln.

Nothing here calls a model. `freigabe_vorbereiten` only reads the Werkstatt and writes one
`angefordert` row; publishing is a separate, deliberate step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import ValidationError

from spaces.learning.services.course_factory.learnhouse_gateway import LearnHouseGatewayError
from spaces.learning.services.course_factory.quality_gate import (
    AktivitaetsSicht,
    LektionsSicht,
    kurs_regeln,
    uses_informal_address,
)
from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.werkstatt.formeltext import klartext
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


def _anrede_befunde(
    texte_je_aufgabe: dict[str, list[str]], aufgaben: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mixed address blocks the release; one finding per task that uses "du", naming it."""
    betroffen = [t for t, texte in texte_je_aufgabe.items() if uses_informal_address(texte)]
    if not betroffen:  # cannot happen while the rule fires, but never drop the block
        return [{"code": "address_mixed", "task_id": None, "blockierend": True,
                 "text": _TEXTE["address_mixed"]}]
    return [
        {"code": "address_mixed", "task_id": t, "blockierend": True,
         "text": f"{_TEXTE['address_mixed']} Betroffen ist die Aufgabe „{aufgaben[t]['summary']}“."}
        for t in betroffen
    ]


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


def _schluessel(
    content: dict[str, Any], aufgaben_key: str | None, lektion_key: str | None
) -> list[dict[str, str]]:
    """The LearnHouse entries a released task content stands for: its lesson (if it carries
    one), then the task, as `{key, art, titel}`. A missing key contributes nothing."""
    inhalt = inhalt_lesen(content)
    eintraege = []
    if inhalt.lektion is not None and lektion_key:
        eintraege.append({"key": lektion_key, "art": "lesson", "titel": inhalt.lektion.title})
    if aufgaben_key:
        eintraege.append({"key": aufgaben_key, "art": "task", "titel": inhalt.prompt[:120]})
    return eintraege


def _vorher_freigegeben(store: WerkstattStore, manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last released manifest, each entry with the LearnHouse entries it put online."""
    return [
        {**m, "schluessel": _schluessel(
            _fassungs_inhalt(store, m["task_id"], m["fassung"]), m["aufgaben_key"], m["lektion_key"]
        )}
        for m in manifest
    ]


def _vorher_uebernommen(
    aufgaben: dict[str, dict[str, Any]], ersten: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Before the first release of a taken-over course: its Fassungen 1, keyed by their
    origin in the published course (what LearnHouse shows right now)."""
    vorher = []
    for task_id, f in ersten.items():
        a = aufgaben[task_id]
        vorher.append({
            "task_id": task_id, "fassung": f["number"], "loesung_hash": loesung_hash(f["content"]),
            "inhalt_hash": inhalt_hash(f["content"]), "kapitel": a["chapter_title"],
            "schluessel": _schluessel(f["content"], a["herkunft_key"], a["lektion_herkunft_key"]),
        })
    return vorher


def _offline(vorher: list[dict[str, Any]], jetzt: set[str]) -> list[dict[str, str]]:
    """Entries online before and missing now (deleted task, lesson removed or moved away)."""
    offline: list[dict[str, str]] = []
    for v in vorher:
        for s in v["schluessel"]:
            if s["key"] not in jetzt and all(o["key"] != s["key"] for o in offline):
                offline.append(s)
    return offline


def _zusammengefuehrt(
    entwurf: Callable[[], CourseDraft] | None, aufgaben: dict[str, dict[str, Any]],
    ausser: set[str], befunde: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Lessons 2..n of the chapters whose lessons were joined into one section at takeover.

    They are still published as factory activities until the first release removes them; an
    unreadable draft only adds a hint (they then stay online)."""
    try:
        if entwurf is None:
            raise LookupError("kein_entwurf")
        draft = entwurf()
    except Exception as error:  # missing/unreadable draft or artifact store: never blocks
        logger.warning(
            "werkstatt_freigabe_entwurf_nicht_lesbar", extra={"fehlerart": type(error).__name__}
        )
        befunde.append({
            "code": "zusammengefuehrte_lektionen_unbekannt", "task_id": None, "blockierend": False,
            "text": "Der veröffentlichte Entwurf des Kurses ließ sich nicht lesen. Lektionen, die "
                    "beim Übernehmen mit der ersten Lektion ihres Kapitels zusammengeführt wurden, "
                    "bleiben deshalb zusätzlich online.",
        })
        return []
    titel = {e.lesson_id: e.title for e in draft.lessons}
    raus: list[dict[str, str]] = []
    for a in aufgaben.values():
        traeger = a.get("lektion_herkunft_key")
        kapitel = next((c for c in draft.chapters if traeger and traeger in c.lesson_ids), None)
        for lesson_id in kapitel.lesson_ids if kapitel else []:
            if (lesson_id != traeger and lesson_id in titel and lesson_id not in ausser
                    and all(r["key"] != lesson_id for r in raus)):
                raus.append({"key": lesson_id, "art": "lesson", "titel": titel[lesson_id]})
    return raus


def _herkunft_fehlt(a: dict[str, Any], erste: dict[str, Any] | None) -> dict[str, Any] | None:
    """A taken-over task without its LearnHouse origin would be written as a second activity."""
    if erste is None:
        return None
    fehlt = []
    if not a["herkunft_key"]:
        fehlt.append("ihrer Aufgabe")
    if (erste["content"].get("lektion") is not None) and not a["lektion_herkunft_key"]:
        fehlt.append("ihrer Lektion")
    if not fehlt:
        return None
    if a.get("deleted"):
        text = (f"Bei der gelöschten übernommenen Aufgabe „{a['summary']}“ fehlt der Bezug zu "
                f"{' und '.join(fehlt)} im veröffentlichten Kurs. Ohne ihn bliebe sie dort "
                "online, obwohl sie gelöscht ist. Bitte zuerst die Herkunft nachtragen.")
    else:
        text = (f"Bei der übernommenen Aufgabe „{a['summary']}“ fehlt der Bezug zu "
                f"{' und '.join(fehlt)} im veröffentlichten Kurs. Eine Freigabe würde sie dort "
                "doppelt anlegen. Bitte zuerst die Herkunft nachtragen.")
    return {"code": "herkunft_fehlt", "task_id": a["task_id"], "blockierend": True, "text": text}


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
    store: WerkstattStore, course_id: str, *, autor: str, learnhouse_revision: int,
    entwurf: Callable[[], CourseDraft] | None = None, neu_ausliefern: bool = False,
) -> dict[str, Any]:
    """Compare with the last release, or, before the first one, with the taken-over Fassungen.

    `vergleich.offline` lists the LearnHouse entries that go offline (not blocking);
    `vergleich.zusammengefuehrt`, on the first release of a taken-over course, the lessons
    joined into one section at takeover, read from the published draft via `entwurf`.
    With `neu_ausliefern`, a course with nothing changed is still releasable: the comparison
    carries `neu_ausliefern`, so the unchanged state is written again (new presentation only).
    It has no effect when anything changed, and findings still block."""
    kurs = store.kurs(course_id)
    aufgaben = {a["task_id"]: a for a in store.aufgaben_alle(course_id)}
    ersten = {
        task_id: f
        for task_id in aufgaben
        if (f := next((f for f in store.fassungen(task_id) if f["art"] == "uebernommen"), None))
    }
    befunde: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    jetzt: set[str] = set()
    sichten: list[AktivitaetsSicht] = []
    lektionen: list[LektionsSicht] = []
    texte_je_aufgabe: dict[str, list[str]] = {}
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
        jetzt.add(aufgaben_key)
        if inhalt.lektion is not None:
            jetzt.add(lektion_key)
        fehlt = _herkunft_fehlt(a, ersten.get(a["task_id"]))
        if fehlt is not None:
            befunde.append(fehlt)
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
        texte_je_aufgabe[a["task_id"]] = [inhalt.prompt]
        if inhalt.lektion is not None:
            lek = _als_lektion(inhalt, inhalt.lektion)
            lektionen.append(LektionsSicht(a["task_id"], tuple(lek.concept_ids), lek.body))
            texte_je_aufgabe[a["task_id"]].append(lek.body)
    for a in aufgaben.values():
        if a["deleted"]:
            fehlt = _herkunft_fehlt(a, ersten.get(a["task_id"]))
            if fehlt is not None:
                befunde.append(fehlt)
    if not kurs["aufgaben"]:
        befunde.append({
            "code": "kurs_ohne_aufgaben", "task_id": None, "blockierend": True,
            "text": "Der Kurs hat noch keine Aufgaben, es gibt nichts zu veröffentlichen.",
        })
    for issue in kurs_regeln(sichten, lektionen, kurs_id=course_id):
        if issue.code == "address_mixed":
            befunde.extend(_anrede_befunde(texte_je_aufgabe, aufgaben))
            continue
        befunde.append({
            "code": issue.code, "task_id": None, "blockierend": issue.code in _KURS_SPERREN,
            "text": _kurs_text(issue.code, issue.subject_id, sichten),
        })
    letzte = store.freigabe_letzte_freigegebene(course_id)
    if letzte is not None:
        vorher = _vorher_freigegeben(store, letzte["manifest"])
    else:
        vorher = _vorher_uebernommen(aufgaben, ersten)
    vergleich = _vergleichen(manifest, vorher, aufgaben)
    vergleich["offline"] = _offline(vorher, jetzt)
    vergleich["zusammengefuehrt"] = []
    if letzte is None and any(a["lektion_herkunft_key"] for a in aufgaben.values()):
        # Only before the first release: afterwards those lessons are already offline.
        vergleich["zusammengefuehrt"] = _zusammengefuehrt(
            entwurf, aufgaben, jetzt | {o["key"] for o in vergleich["offline"]}, befunde
        )
    blockiert = any(b["blockierend"] for b in befunde)
    nichts = not any(vergleich[k] for k in ("neu", "geaendert", "textkorrektur", "geloescht"))
    if nichts and neu_ausliefern:
        vergleich["neu_ausliefern"] = True
        nichts = False
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
        kapitel: list[dict[str, Any]], entfernen: list[str] | None = None, math: bool = False,
    ) -> dict[str, Any]: ...


class _Katalog(Protocol):
    def letzte_revision(self, course_id: str, *, unter: int) -> int | None: ...

    def hat_revision(self, course_id: str, revision: int) -> bool: ...

    def projizieren(
        self, course_id: str, course_revision: int, eintraege: list[dict[str, Any]],
        vorher_revision: int | None, vorher_eintraege: list[dict[str, Any]] | None = None,
        *, fach: str = "terminal",
    ) -> dict[str, Any]: ...


def _ist_mathe(store: WerkstattStore, course_id: str) -> bool:
    return store.kurs(course_id).get("fach") == "mathe"


def _fassungs_inhalt(store: WerkstattStore, task_id: str, number: int) -> dict[str, Any]:
    fassung = next((f for f in store.fassungen(task_id) if f["number"] == number), None)
    if fassung is None:
        raise ValueError("manifest_fassung_fehlt")
    return fassung["content"]


_TITEL_LAENGE = 120
_TITEL_FORMEL = re.compile(r"\$\$(.+?)\$\$|(?<!\\)\$([^$]+?)(?<!\\)\$", re.S)


def aktivitaetstitel(prompt: str, *, mathe: bool) -> str:
    """The LearnHouse activity name of a task: its prompt, cut to 120 characters.

    In a math course the formulas lose their dollars (a name is plain text) and the cut never
    falls inside a formula: a formula that does not fit is left out whole. Terminal courses keep
    the plain cut, `$env:...` included."""
    if not mathe:
        return prompt[:_TITEL_LAENGE]
    titel, position = "", 0
    for treffer in _TITEL_FORMEL.finditer(prompt):
        text = prompt[position:treffer.start()].replace("\\$", "$")
        formel = klartext("$" + (treffer.group(1) or treffer.group(2)) + "$")
        if len(titel) + len(text) + len(formel) > _TITEL_LAENGE:
            return (titel + text)[:_TITEL_LAENGE].rstrip()
        titel += text + formel
        position = treffer.end()
    return (titel + prompt[position:].replace("\\$", "$"))[:_TITEL_LAENGE]


def _kapitel_aus_manifest(
    store: WerkstattStore, manifest: list[dict[str, Any]], *, mathe: bool = False
) -> list[dict[str, Any]]:
    """Chapters in manifest order; each task's lesson first. Reads the approved version, not the newest."""
    kapitel: dict[str, dict[str, Any]] = {}
    for m in sorted(manifest, key=lambda e: e["position"]):
        inhalt = inhalt_lesen(_fassungs_inhalt(store, m["task_id"], m["fassung"]))
        eintraege = kapitel.setdefault(
            m["kapitel"], {"titel": klartext(m["kapitel"]) if mathe else m["kapitel"], "eintraege": []}
        )["eintraege"]
        if inhalt.lektion is not None:
            eintraege.append({
                "key": m["lektion_key"], "art": "lesson", "titel": klartext(inhalt.lektion.title) if mathe else inhalt.lektion.title,
                "markdown": inhalt.lektion.body,
                "claims": [c.text for c in inhalt.lektion.claims],
            })
        eintraege.append({
            "key": m["aufgaben_key"], "art": "task",
            "titel": aktivitaetstitel(inhalt.prompt, mathe=mathe), "prompt": inhalt.prompt,
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
    fach = "mathe" if _ist_mathe(store, course_id) else "terminal"
    return katalog.projizieren(
        course_id, revision, _mit_inhalt(store, freigabe["manifest"]), vorher_revision,
        vorher_eintraege=vorher, fach=fach,
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

    On a gateway error the release stays `angefordert`. If LearnHouse already stands one
    revision further (the answer to our own write may have been lost), the very same write is
    sent again with the old expected revision: LearnHouse answers a known `freigabe_id` with
    the stored result, and refuses with `revision_conflict` if someone else moved the course.
    With `katalog`, the practice catalog of the new revision is projected afterwards; a catalog
    failure is logged and reported as `katalog_fehler`, it never undoes the release. No model
    is called."""
    freigabe = store.freigabe(freigabe_id)
    if freigabe["zustand"] != "angefordert":
        raise ValueError("freigabe_nicht_offen")
    vorher = freigabe["learnhouse_revision_vorher"]
    if vorher is None or learnhouse_revision not in (vorher, vorher + 1):
        raise ValueError("learnhouse_stand_veraltet")
    if any(b.get("blockierend") for b in freigabe["befunde"]):
        raise ValueError("freigabe_blockiert")
    vergleich = freigabe["vergleich"]
    if not vergleich.get("neu_ausliefern") and not any(
        vergleich.get(k) for k in ("neu", "geaendert", "textkorrektur", "geloescht")
    ):
        raise ValueError("nichts_zu_veroeffentlichen")
    mathe = _ist_mathe(store, freigabe["course_id"])
    kapitel = _kapitel_aus_manifest(store, freigabe["manifest"], mathe=mathe)
    entfernen = list(dict.fromkeys(
        e["key"] for k in ("offline", "zusammengefuehrt") for e in vergleich.get(k) or []
    ))
    # Formula typesetting in the lesson pages is opt-in (Ruling 6): terminal lessons carry
    # `$env:...` and `$HOME`, so only math courses send the switch.
    extra: dict[str, Any] = {"math": True} if mathe else {}
    try:
        antwort = await gateway.werkstatt_stand_schreiben(
            freigabe["course_id"], expected_revision=vorher, freigabe_id=freigabe_id,
            kapitel=kapitel, entfernen=entfernen, **extra,
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
