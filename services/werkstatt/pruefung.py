"""Fixed checks of one Werkstatt task. No model: the same rules as the course factory's
quality gate, applied to a single task, each finding with a sentence for the chat."""

from __future__ import annotations

import re
from typing import Any, TypedDict
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
from spaces.learning.services.werkstatt.formeln import formel_fehler
from spaces.learning.services.werkstatt.inhalt import (
    AufgabeInhalt,
    Fach,
    Fall,
    Lektionsabschnitt,
    Luecke,
    Offen,
    Quiz,
    Reihenfolge,
    Zahl,
    Zuordnung,
)
from spaces.learning.services.werkstatt.nachrechnen import (
    ERLAUBTE_NAMEN,
    MAX_ZIFFERN,
    NachrechnenFehler,
    nachrechnen,
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
    "quellenschluessel_im_text": "Im Text, in der Aufgabenstellung oder in der Antwort stehen Quellenschlüssel wie „(S12)“; sie gehören nur in die Quellenangaben, nicht in den Text.",
    "codeblock_ohne_shell": "Ein Codeblock hat keine Sprachangabe oder eine andere als powershell, cmd oder bash.",
    "lektion_kuendigt_an": "Der Lektionstext kündigt an, was er oder die Aufgaben tun werden (etwa „In diesem Abschnitt lernen Sie …“), statt die Inhalte direkt zu vermitteln.",
    "claim_uncited": "Eine Aussage der Lektion hat keine Quelle, die sie belegt.",
    "command_not_in_source": "Ein Befehl im Codeblock steht nirgends wörtlich in den Quellen.",
    "cloze_blank_missing": "Im Lückentext fehlt zu einem Platzhalter die passende Lücke.",
    "cloze_blank_empty": "Eine Lücke hat keine zulässige Antwort.",
    "ordering_duplicate": "In der Reihenfolge kommt ein Schritt doppelt vor.",
    "matching_duplicate": "In der Zuordnung kommt ein Begriff doppelt vor.",
    "formel_ungueltig": "Eine Formel ist nicht schreibbar: ein „$“ wird nicht geschlossen, Klammern sind nicht ausgeglichen oder ein Formelbefehl ist unbekannt.",
    "luecke_mit_term": "Eine Lücke erwartet einen Term; Lücken sind nur für Zahlen oder einzelne Wörter, Terme gehören in eine offene Aufgabe.",
    "zahl_falsch": "Die angegebene Lösung stimmt nicht mit dem Ergebnis der Rechnung überein; bitte Lösung oder Rechnung berichtigen.",
    # Tells the author model what is allowed, so the repair round can succeed.
    "zahl_unpruefbar": (
        "Die Rechnung zur Lösung lässt sich nicht nachrechnen; erlaubt sind nur Zahlen bis "
        f"{MAX_ZIFFERN} Stellen, x, + - * / ^ ohne Potenztürme wie 2^3^4, runde Klammern, "
        "Ausdruck.subs(x, Wert), N(…) nur als Funktion und "
        + ", ".join(sorted(ERLAUBTE_NAMEN - {"x", "subs", "N"}, key=str.lower))
        + ", und das Ergebnis muss schnell eine endliche reelle Zahl ergeben."
    ),
}


# A source key used as a citation in running text: S<n> inside round or square brackets,
# alone or in a list ("(S12)", "[S2, S3]"). Bare "S3" elsewhere (words, "Seite (S. 3)") is not
# matched; fenced code is cut out first.
_SCHLUESSEL_IM_TEXT = re.compile(r"[(\[]\s*S\d+(?:\s*[,;/]\s*S\d+)*\s*[)\]]")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_ERLAUBTE_SHELLS = {"powershell", "cmd", "bash"}
# An announcement at a sentence start (text start, line start after optional heading marks, or
# after . ! ? :). The course factory has no wording rule for it (its quality gate only measures
# body coverage of the claims), so the common German openings are listed here.
_ANKUENDIGUNG = re.compile(
    r"(?:^|(?<=[.!?:])[ \t]+|(?<=\n))[ \t]*(?:#+[ \t]*)?"
    r"(?:in\s+diesem\s+(?:abschnitt|kapitel|teil)\b|in\s+dieser\s+(?:lektion|einheit)\b"
    r"|im\s+folgenden\b|in\s+den\s+folgenden\s+aufgaben\b|dieser\s+abschnitt\b)",
    re.I | re.M,
)


def _ohne_code(text: str) -> str:
    return _FENCE.sub(" ", text)


def _codeblock_tags(text: str) -> list[str]:
    tags: list[str] = []
    offen = False
    for zeile in text.splitlines():
        s = zeile.strip()
        if not s.startswith("```"):
            continue
        if offen:
            offen = False
        else:
            offen = True
            teile = s[3:].strip().split()
            tags.append(teile[0].lower() if teile else "")
    return tags


def _text_codes(inhalt: AufgabeInhalt, fach: Fach = "terminal") -> list[str]:
    texte = [inhalt.prompt, getattr(inhalt, "expected_answer", "") or ""]
    if isinstance(inhalt, Quiz):
        texte += [o.text for o in inhalt.options]
    body = inhalt.lektion.body if inhalt.lektion is not None else ""
    texte.append(body)
    codes: list[str] = []
    if any(_SCHLUESSEL_IM_TEXT.search(_ohne_code(t)) for t in texte):
        codes.append("quellenschluessel_im_text")
    if fach == "terminal" and any(
        tag not in _ERLAUBTE_SHELLS for t in texte for tag in _codeblock_tags(t)
    ):
        codes.append("codeblock_ohne_shell")
    if _ANKUENDIGUNG.search(_ohne_code(body)):
        codes.append("lektion_kuendigt_an")
    return codes


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
    if isinstance(inhalt, Zahl):
        return _zahl_codes(inhalt)
    return []


def _zahl_codes(inhalt: Zahl) -> list[str]:
    """Recompute the calculation (child process, time limit); the stated value must match it."""
    try:
        ergebnis = nachrechnen(inhalt.rechnung)
    except NachrechnenFehler:
        return ["zahl_unpruefbar"]
    return [] if abs(ergebnis - inhalt.value) <= inhalt.tolerance else ["zahl_falsch"]


def _platzhalter_ok(inhalt: Luecke) -> bool:
    return set(re.findall(r"\{\{([^{}]+)\}\}", inhalt.template)) == {b.blank_id for b in inhalt.blanks}


_REINE_ZAHL = re.compile(r"[+-]?\d+(?:[.,]\d+)?")


def _formel_texte(inhalt: AufgabeInhalt) -> list[str]:
    texte = [inhalt.prompt, getattr(inhalt, "expected_answer", "") or ""]
    if isinstance(inhalt, Quiz):
        texte += [o.text for o in inhalt.options]
    if isinstance(inhalt, Zuordnung):
        texte += [t for p in inhalt.pairs for t in (p.left, p.right)]
    if isinstance(inhalt, Reihenfolge):
        texte += inhalt.items
    if isinstance(inhalt, Luecke):
        texte.append(inhalt.template)
    if inhalt.lektion is not None:
        texte.append(inhalt.lektion.body)
    return texte


_MINUS_ZWISCHEN_BUCHSTABEN = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")


def _ist_term(wert: str) -> bool:
    """A cloze answer is a term if it has an operator; `-` between two letters is a hyphen
    ("x-Achse"), and a number such as "-1,5" stays a number."""
    if _REINE_ZAHL.fullmatch(wert):
        return False
    rest = _MINUS_ZWISCHEN_BUCHSTABEN.sub("", wert)
    return any(z in rest for z in "+-*/^=()")


def _mathe_codes(inhalt: AufgabeInhalt) -> list[str]:
    codes: list[str] = []
    if any(formel_fehler(t) for t in _formel_texte(inhalt)):
        codes.append("formel_ungueltig")
    if isinstance(inhalt, Luecke):
        for blank in inhalt.blanks:
            for a in blank.accepted:
                wert = a.strip()
                if _ist_term(wert):
                    codes.append("luecke_mit_term")
    return codes


def aufgabe_pruefen(
    inhalt: AufgabeInhalt, *, sources: tuple[SourceSnapshot, ...], fach: Fach = "terminal"
) -> list[Befund]:
    codes = _aufgabe_codes(inhalt)
    texte = [inhalt.prompt]
    abschnitt = inhalt.lektion
    if abschnitt is not None:
        codes += [i.code for i in lesson_teaching_issues(_als_lektion(inhalt, abschnitt))]
        texte.append(abschnitt.body)
    if mixes_address(texte):
        codes.append("address_mixed")
    if abschnitt is not None:
        # A claim counts as cited only through a citation object of this task.
        zitiert = {z.citation_id for z in inhalt.zitate}
        if any(not zitiert.intersection(claim.citation_ids) for claim in abschnitt.claims):
            codes.append("claim_uncited")
        if fach == "terminal" and subjects_with_unsourced_command([("lektion", abschnitt.body)], sources):
            codes.append("command_not_in_source")
    codes += _text_codes(inhalt, fach)
    if fach == "mathe":
        codes += _mathe_codes(inhalt)
    return [_befund(code) for code in dict.fromkeys(codes)]


def _ref_schluessel(ref: dict[str, Any]) -> tuple[str, int, str, str]:
    return (
        str(ref["source_id"]), int(ref["source_revision"]), str(ref["chunk_id"]),
        str(ref["chunk_content_hash"]),
    )


def _zitat_schluessel(zitat: Citation) -> tuple[str, int, str, str]:
    return (zitat.source_id, zitat.source_revision, zitat.chunk_id, zitat.content_hash)


def quellen_aufloesen(
    inhalt: AufgabeInhalt, source_refs: tuple[str, ...], *, course_id: str
) -> tuple[AufgabeInhalt, list[Citation]]:
    """S1.. keys become exact citations (uuid5 of course_id:chunk_id); unknown keys are dropped.

    A citation already in `zitate` counts only if source, revision, chunk and content hash
    match a chunk of the CURRENT catalog; any other id (made up, stale revision, changed
    chunk) is dropped, so a claim left without one is `claim_uncited`. `zitate` is rebuilt
    from what is still referenced."""
    keyed = {ref["key"]: ref for ref in keyed_source_refs(source_refs)}
    aktuell = {_ref_schluessel(ref) for ref in keyed.values()}
    bekannt = {
        z.citation_id: z for z in inhalt.zitate if _zitat_schluessel(z) in aktuell
    }
    # A key naming a chunk that is already cited reuses that citation instead of a twin.
    je_chunk = {_zitat_schluessel(z): z.citation_id for z in bekannt.values()}
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
            vorhanden = je_chunk.get(_ref_schluessel(ref))
            if vorhanden is not None:
                out.append(vorhanden)
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


def mit_quellenschluesseln(content: dict[str, Any], source_refs: tuple[str, ...]) -> dict[str, Any]:
    """A stored version as the model may see it: citation ids become the S-keys of the current
    catalog, ids that match no current chunk are dropped, and the `zitate` objects go."""
    keyed = keyed_source_refs(source_refs)
    schluessel = {ref["key"] for ref in keyed}
    je_chunk = {_ref_schluessel(ref): ref["key"] for ref in keyed}
    nach_key: dict[str, str] = {}
    for roh in content.get("zitate") or []:
        try:
            zitat = Citation.model_validate(roh)
        except ValueError:
            continue
        key = je_chunk.get(_zitat_schluessel(zitat))
        if key is not None:
            nach_key[zitat.citation_id] = key

    def abbilden(ids: list[str]) -> list[str]:
        out = [nach_key.get(i) or (i if i in schluessel else None) for i in ids]
        return list(dict.fromkeys(i for i in out if i is not None))

    ergebnis = {k: v for k, v in content.items() if k != "zitate"}
    if "citation_ids" in ergebnis:
        ergebnis["citation_ids"] = abbilden(list(ergebnis["citation_ids"]))
    lektion = ergebnis.get("lektion")
    if isinstance(lektion, dict):
        ergebnis["lektion"] = {
            **lektion,
            "claims": [
                {**c, "citation_ids": abbilden(list(c.get("citation_ids", [])))}
                for c in lektion.get("claims", [])
            ],
        }
    return ergebnis
