"""Fixed checks of one Werkstatt task. No model: the same rules as the course factory's
quality gate, applied to a single task, each finding with a sentence for the chat."""

from __future__ import annotations

import re
from typing import TypedDict
from uuid import NAMESPACE_URL, uuid5

from spaces.learning.services.course_factory.chapter_verification import keyed_source_refs
from spaces.learning.services.course_factory.quality_gate import (
    activity_issues,
    lesson_teaching_issues,
    mixes_address,
)
from spaces.learning.services.course_factory.schemas import ActivityDraft, Citation, LessonDraft
from spaces.learning.services.course_factory.verification import (
    SourceSnapshot,
    subjects_with_unsourced_command,
)
from spaces.learning.services.werkstatt.inhalt import (
    AufgabeInhalt,
    Fall,
    Lektionsabschnitt,
    Luecke,
    Offen,
    Quiz,
    Reihenfolge,
    Zuordnung,
)


class Befund(TypedDict):
    code: str
    text: str


_TEXTE = {
    "expected_answer_missing": "Die Aufgabe hat keine Musterantwort, daher lässt sich eine Antwort nicht beurteilen.",
    "rubric_missing": "Die Aufgabe hat keine Bewertungskriterien.",
    "quiz_options_invalid": "Die Antwortoptionen sind nicht stimmig, denn sie sind doppelt oder die richtige Option fehlt.",
    "quiz_all_but_one": "Alle Optionen bis auf eine sind richtig, so lässt sich die Aufgabe durch Raten der falschen Option lösen.",
    "lesson_contains_tasks": "Im Lektionstext stehen Aufgaben, Optionen oder Lösungen, der Text soll aber nur lehren.",
    "lesson_body_thin": "Der Lektionstext kündigt den Inhalt nur an und vermittelt die Aussagen nicht selbst.",
    "lesson_command_missing": "Ein Befehl, den eine Aussage nennt, steht nicht wörtlich im Lektionstext.",
    "address_mixed": "Die Anrede ist uneinheitlich, denn „du“ und „Sie“ kommen gemischt vor; bitte durchgehend „Sie“ verwenden.",
    "claim_uncited": "Eine Aussage der Lektion hat keine Quelle, die sie belegt.",
    "command_not_in_source": "Ein Befehl im Codeblock steht nirgends wörtlich in den Quellen.",
    "cloze_blank_missing": "Im Lückentext fehlt zu einem Platzhalter die passende Lücke.",
    "cloze_blank_empty": "Eine Lücke hat keine zulässige Antwort.",
    "ordering_duplicate": "In der Reihenfolge kommt ein Schritt doppelt vor.",
    "matching_duplicate": "In der Zuordnung kommt ein Begriff doppelt vor.",
}


def _befund(code: str) -> Befund:
    return {"code": code, "text": _TEXTE.get(code, "Die Prüfung hat ein Problem gefunden.")}


def _als_aktivitaet(inhalt: Quiz | Offen | Fall) -> ActivityDraft:
    if isinstance(inhalt, Quiz):
        return ActivityDraft(
            activity_id="aufgabe", chapter_id="kapitel", type="quiz", concept_ids=inhalt.concept_ids,
            prompt=inhalt.prompt, expected_answer="", citation_ids=inhalt.citation_ids, rubric=[],
            options=inhalt.options, correct_option_ids=inhalt.correct_option_ids,
        )
    return ActivityDraft(
        activity_id="aufgabe", chapter_id="kapitel", type=inhalt.kind, concept_ids=inhalt.concept_ids,
        prompt=inhalt.prompt, expected_answer=inhalt.expected_answer, citation_ids=inhalt.citation_ids,
        rubric=inhalt.rubric,
    )


def _als_lektion(inhalt: AufgabeInhalt, abschnitt: Lektionsabschnitt) -> LessonDraft:
    return LessonDraft(
        lesson_id="lektion", chapter_id="kapitel", title=abschnitt.title, body=abschnitt.body,
        concept_ids=abschnitt.concept_ids or inhalt.concept_ids, claims=abschnitt.claims,
    )


def _aufgabe_codes(inhalt: AufgabeInhalt) -> list[str]:
    if isinstance(inhalt, (Quiz, Offen, Fall)):
        return [i.code for i in activity_issues(_als_aktivitaet(inhalt))]
    if isinstance(inhalt, Luecke):
        codes: list[str] = []
        if not _platzhalter_ok(inhalt):
            codes.append("cloze_blank_missing")
        if any(not any(a.strip() for a in b.accepted) for b in inhalt.blanks):
            codes.append("cloze_blank_empty")
        return codes
    if isinstance(inhalt, Reihenfolge):
        gesehen = [i.strip().lower() for i in inhalt.items]
        return ["ordering_duplicate"] if len(set(gesehen)) != len(gesehen) else []
    if isinstance(inhalt, Zuordnung):
        links = [p.left.strip().lower() for p in inhalt.pairs]
        rechts = [p.right.strip().lower() for p in inhalt.pairs]
        if len(set(links)) != len(links) or len(set(rechts)) != len(rechts):
            return ["matching_duplicate"]
    return []


def _platzhalter_ok(inhalt: Luecke) -> bool:
    return set(re.findall(r"\{\{([^{}]+)\}\}", inhalt.template)) == {b.blank_id for b in inhalt.blanks}


def aufgabe_pruefen(inhalt: AufgabeInhalt, *, sources: tuple[SourceSnapshot, ...]) -> list[Befund]:
    codes = _aufgabe_codes(inhalt)
    texte = [inhalt.prompt]
    abschnitt = inhalt.lektion
    if abschnitt is not None:
        codes += [i.code for i in lesson_teaching_issues(_als_lektion(inhalt, abschnitt))]
        texte.append(abschnitt.body)
    if mixes_address(texte):
        codes.append("address_mixed")
    if abschnitt is not None:
        if any(not claim.citation_ids for claim in abschnitt.claims):
            codes.append("claim_uncited")
        if subjects_with_unsourced_command([("lektion", abschnitt.body)], sources):
            codes.append("command_not_in_source")
    return [_befund(code) for code in dict.fromkeys(codes)]


def quellen_aufloesen(
    inhalt: AufgabeInhalt, source_refs: tuple[str, ...], *, course_id: str
) -> tuple[AufgabeInhalt, list[Citation]]:
    """S1.. keys become exact citations (uuid5 of course_id:chunk_id); unknown keys are dropped."""
    keyed = {ref["key"]: ref for ref in keyed_source_refs(source_refs)}
    bekannt = {z.citation_id: z for z in inhalt.zitate}
    gemacht: dict[str, Citation] = dict(bekannt)

    def aufloesen(ids: list[str]) -> list[str]:
        out: list[str] = []
        for wert in ids:
            if wert in bekannt:
                out.append(wert)
                continue
            ref = keyed.get(wert)
            if ref is None:
                continue
            zitat = Citation(
                citation_id=str(uuid5(NAMESPACE_URL, f"{course_id}:{ref['chunk_id']}")),
                source_id=ref["source_id"],
                source_revision=ref["source_revision"],
                chunk_id=ref["chunk_id"],
                content_hash=ref["chunk_content_hash"],
                locator=ref["locator"],
            )
            gemacht.setdefault(zitat.citation_id, zitat)
            out.append(zitat.citation_id)
        return list(dict.fromkeys(out))

    update: dict[str, object] = {"citation_ids": aufloesen(inhalt.citation_ids)}
    if inhalt.lektion is not None:
        claims = [c.model_copy(update={"citation_ids": aufloesen(c.citation_ids)}) for c in inhalt.lektion.claims]
        update["lektion"] = inhalt.lektion.model_copy(update={"claims": claims})
    zitate = list(gemacht.values())
    # Only citations still referenced stay: a dropped key leaves nothing behind.
    benutzt = set(update["citation_ids"])  # type: ignore[arg-type]
    if "lektion" in update:
        benutzt |= {i for c in update["lektion"].claims for i in c.citation_ids}  # type: ignore[attr-defined]
    zitate = [z for z in zitate if z.citation_id in benutzt]
    update["zitate"] = zitate
    return inhalt.model_copy(update=update), zitate
