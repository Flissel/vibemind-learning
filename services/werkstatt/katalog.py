"""Practice catalog ("Üben") per Werkstatt release, projected from the released task contents.

One engine item per released task, in the shapes the factory catalog uses (choice and rubric
from ``course_factory.practice_catalog``, fill_in/ordering/matching from
``course_factory.structured_items``), so the fork's TaskRenderer shows them unchanged.

Learner state carries over per concept: if the set of solution hashes of the tasks attached to
a concept is the same as in the previous release, mastery, review schedule and misconceptions
are copied from the old concept id to the new one (the old rows stay, for sessions still
running on the old revision). Nothing here calls a model.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ConceptMastery,
    ItemConcept,
    Misconception,
    ReviewSchedule,
)
from spaces.learning.services.course_factory.practice_catalog import (
    _FALLBACK_CRITERION,
    STARTING_DIFFICULTY,
    _scaled,
    _section_hints,
    _source_refs,
    _stable_id,
)
from spaces.learning.services.lernsequenz.aus_struktur import _media_available, _random_ids
from spaces.learning.services.lernsequenz.bilder import render_terminal
from spaces.learning.services.lernsequenz.medien import save_media
from spaces.learning.services.werkstatt.inhalt import (
    AufgabeInhalt,
    Fall,
    Luecke,
    Offen,
    Quiz,
    Reihenfolge,
    Zuordnung,
    inhalt_lesen,
)
from spaces.learning.services.werkstatt.models import WerkstattKurs
from spaces.learning.services.zufall import anders_gemischt, gemischt

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

_PLATZHALTER = re.compile(r"\{\{([^{}]+)\}\}")

# Shown order of ordering steps and matching answers. Not seeded from anything public: a
# permutation recomputable from the item id (this code is public) would hand out the solution.
_ZUFALL = secrets.SystemRandom()


def _gemischt(eintraege: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A shuffled copy that never shows the authored (= solution) order while it can differ."""
    return anders_gemischt(eintraege, lambda e: e["label"], _ZUFALL)


def _hashes_je_konzept(eintraege: list[dict[str, Any]]) -> dict[str, frozenset[str]]:
    je_konzept: dict[str, set[str]] = {}
    for e in eintraege:
        for key in inhalt_lesen(e["content"]).concept_ids:
            je_konzept.setdefault(key, set()).add(e["loesung_hash"])
    return {key: frozenset(h) for key, h in je_konzept.items()}


class WerkstattKatalog:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def letzte_revision(self, course_id: str, *, unter: int) -> int | None:
        """The newest catalog revision of the course below ``unter`` (e.g. the factory's)."""
        with self._session_factory() as session:
            return session.scalar(
                select(func.max(AdaptiveConcept.course_revision)).where(
                    AdaptiveConcept.course_id == course_id,
                    AdaptiveConcept.course_revision < unter,
                )
            )

    def hat_revision(self, course_id: str, revision: int) -> bool:
        with self._session_factory() as session:
            return session.scalar(
                select(AdaptiveConcept.id).where(
                    AdaptiveConcept.course_id == course_id,
                    AdaptiveConcept.course_revision == revision,
                ).limit(1)
            ) is not None

    def projizieren(
        self,
        course_id: str,
        course_revision: int,
        eintraege: list[dict[str, Any]],
        vorher_revision: int | None,
        vorher_eintraege: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """Write the catalog of ``course_revision`` and carry learner state over.

        ``eintraege``: released manifest entries, each with ``aufgaben_key``, ``loesung_hash``
        and the released ``content``. ``vorher_eintraege``: the same for the release that
        ``vorher_revision``'s catalog was projected from; without it nothing carries over.
        Idempotent: a revision that already has a catalog is not written again, and learner
        state is only copied where the new concept has none yet."""
        inhalte = [(e, inhalt_lesen(e["content"])) for e in eintraege]
        with self._session_factory() as session, session.begin():
            vorhanden = session.scalar(
                select(AdaptiveConcept.id).where(
                    AdaptiveConcept.course_id == course_id,
                    AdaptiveConcept.course_revision == course_revision,
                ).limit(1)
            )
            items = konzepte = 0
            if vorhanden is None:
                items, konzepte = self._katalog_schreiben(
                    session, course_id, course_revision, inhalte, vorher_revision
                )
            uebernommen = 0
            if vorher_revision is not None and vorher_eintraege is not None:
                uebernommen = self._lernstand_uebernehmen(
                    session, course_id, course_revision, vorher_revision,
                    _hashes_je_konzept(vorher_eintraege), _hashes_je_konzept(eintraege),
                )
            return {"items": items, "konzepte": konzepte, "lernstand_uebernommen": uebernommen}

    # catalog

    def _katalog_schreiben(
        self, session: Session, course_id: str, course_revision: int,
        inhalte: list[tuple[dict[str, Any], AufgabeInhalt]], vorher_revision: int | None,
    ) -> tuple[int, int]:
        titel_vorher: dict[str, str] = {}
        if vorher_revision is not None:
            titel_vorher = dict(session.execute(
                select(AdaptiveConcept.concept_key, AdaptiveConcept.title).where(
                    AdaptiveConcept.course_id == course_id,
                    AdaptiveConcept.course_revision == vorher_revision,
                )
            ).tuples().all())
        keys = list(dict.fromkeys(k for _e, inhalt in inhalte for k in inhalt.concept_ids))
        konzept_ids = {k: _stable_id(course_id, course_revision, "concept", k) for k in keys}
        for key in keys:
            session.add(AdaptiveConcept(
                id=konzept_ids[key], course_id=course_id, course_revision=course_revision,
                concept_key=key, title=titel_vorher.get(key, key),
            ))
        session.flush()
        refs = self._quellen(session, course_id, [inhalt for _e, inhalt in inhalte])
        for eintrag, inhalt in inhalte:
            key = eintrag["aufgaben_key"]
            item_id = _stable_id(course_id, course_revision, "item", key)
            cited = list(dict.fromkeys(refs[c] for c in inhalt.citation_ids if c in refs))
            item_type, erwartet, config, rubrik = _item_felder(item_id, inhalt)
            session.add(AdaptiveItem(
                id=item_id, course_id=course_id, course_revision=course_revision,
                activity_id=key, item_type=item_type, difficulty=STARTING_DIFFICULTY,
                # The release passed the Werkstatt checks; blocking findings stop it earlier.
                quality_status="approved", expected_answer=erwartet,
                scoring_config={
                    "source_refs": cited, "hints": _section_hints(cited), "options": [],
                    "presentation": {}, **config,
                },
                rubric=rubrik,
            ))
            session.flush()
            for konzept in dict.fromkeys(inhalt.concept_ids):
                session.add(ItemConcept(
                    item_id=item_id, concept_id=konzept_ids[konzept],
                    course_id=course_id, course_revision=course_revision,
                ))
        session.flush()
        return len(inhalte), len(keys)

    @staticmethod
    def _quellen(session: Session, course_id: str, inhalte: list[AufgabeInhalt]) -> dict[str, str]:
        """Citation id -> the canonical source reference, exactly as the factory catalog builds it."""
        kurs = session.get(WerkstattKurs, course_id)
        provenance = [
            SimpleNamespace(source_id=str(p["source_id"]), content_hash=str(p["content_hash"]))
            for p in (kurs.source_provenance if kurs is not None else [])
        ]
        zitate = list({z.citation_id: z for inhalt in inhalte for z in inhalt.zitate}.values())
        return _source_refs(session, SimpleNamespace(source_provenance=provenance, citations=zitate))  # type: ignore[arg-type]

    # learner state

    def _lernstand_uebernehmen(
        self, session: Session, course_id: str, course_revision: int, vorher_revision: int,
        alt: dict[str, frozenset[str]], neu: dict[str, frozenset[str]],
    ) -> int:
        """Copy state of every concept whose task solutions are unchanged; returns the number
        of (learner, concept) pairs that got state copied in this call."""
        def ids(revision: int) -> dict[str, str]:
            return dict(session.execute(
                select(AdaptiveConcept.concept_key, AdaptiveConcept.id).where(
                    AdaptiveConcept.course_id == course_id,
                    AdaptiveConcept.course_revision == revision,
                )
            ).tuples().all())

        alte_ids, neue_ids = ids(vorher_revision), ids(course_revision)
        paare: set[tuple[str, str]] = set()
        for key, hashes in neu.items():
            if key not in alte_ids or key not in neue_ids or alt.get(key) != hashes:
                continue
            von, nach = alte_ids[key], neue_ids[key]
            for m in session.scalars(select(ConceptMastery).where(ConceptMastery.concept_id == von)).all():
                if session.get(ConceptMastery, (m.actor_id, nach)) is None:
                    session.add(ConceptMastery(
                        actor_id=m.actor_id, concept_id=nach, alpha=m.alpha, beta=m.beta,
                        revision=m.revision, updated_at=m.updated_at,
                    ))
                    paare.add((m.actor_id, nach))
            for r in session.scalars(select(ReviewSchedule).where(ReviewSchedule.concept_id == von)).all():
                if session.get(ReviewSchedule, (r.actor_id, nach)) is None:
                    session.add(ReviewSchedule(
                        actor_id=r.actor_id, concept_id=nach, interval_index=r.interval_index,
                        due_at=r.due_at, maintenance=r.maintenance,
                        alternate_representation_required=r.alternate_representation_required,
                        last_evaluation_id=r.last_evaluation_id, revision=r.revision,
                        updated_at=r.updated_at,
                    ))
                    paare.add((r.actor_id, nach))
            for f in session.scalars(select(Misconception).where(Misconception.concept_id == von)).all():
                schon = session.scalar(select(Misconception.id).where(
                    Misconception.actor_id == f.actor_id, Misconception.concept_id == nach,
                    Misconception.tag == f.tag,
                ))
                if schon is None:
                    session.add(Misconception(
                        id=str(uuid5(NAMESPACE_URL, f"learning-practice:lernstand:{f.id}:{nach}")),
                        actor_id=f.actor_id, concept_id=nach, tag=f.tag, status=f.status,
                        first_evaluation_id=f.first_evaluation_id,
                        latest_evaluation_id=f.latest_evaluation_id,
                        created_at=f.created_at, resolved_at=f.resolved_at,
                    ))
                    paare.add((f.actor_id, nach))
            session.flush()
        return len(paare)


# item shapes


def _item_felder(
    item_id: str, inhalt: AufgabeInhalt
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """``(item_type, expected_answer, scoring_config, rubric)`` for one released task."""
    if isinstance(inhalt, Quiz):
        optionen = [{"id": o.option_id, "label": o.text} for o in inhalt.options]
        optionen = gemischt(optionen)
        richtig = list(inhalt.correct_option_ids)
        if len(richtig) == 1:
            return "single_choice", {"choice_id": richtig[0]}, {"prompt": inhalt.prompt, "options": optionen}, []
        return "multiple_choice", {"choice_ids": richtig}, {"prompt": inhalt.prompt, "options": optionen}, []
    if isinstance(inhalt, (Offen, Fall)):
        rubrik = _scaled([c.model_dump(mode="json") for c in inhalt.rubric])
        return (
            inhalt.kind, {"text": inhalt.expected_answer or inhalt.prompt}, {"prompt": inhalt.prompt},
            rubrik or [dict(_FALLBACK_CRITERION)],
        )
    if isinstance(inhalt, Luecke):
        return _luecke(inhalt)
    if isinstance(inhalt, Reihenfolge):
        ids = _random_ids("s", len(inhalt.items))
        optionen = _gemischt([{"id": i, "label": text} for i, text in zip(ids, inhalt.items)])
        return "ordering", {"order": ids}, {"prompt": inhalt.prompt, "options": optionen, "partial_credit": True}, []
    if isinstance(inhalt, Zuordnung):
        links = [{"id": f"b{n}", "label": p.left} for n, p in enumerate(inhalt.pairs, start=1)]
        rechts_ids = _random_ids("e", len(inhalt.pairs))
        rechts = _gemischt([{"id": i, "label": p.right} for i, p in zip(rechts_ids, inhalt.pairs)])
        return (
            "matching", {"pairs": {f"b{n}": i for n, i in enumerate(rechts_ids, start=1)}},
            {"prompt": inhalt.prompt, "partial_credit": True, "presentation": {"left": links, "right": rechts}},
            [],
        )
    raise TypeError(type(inhalt).__name__)


def _luecke(inhalt: Luecke) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if len(inhalt.blanks) > 1:
        # The engine scores one text per fill_in; several blanks are answered as one text.
        loesung = {b.blank_id: b.accepted[0] for b in inhalt.blanks}
        luecken = _PLATZHALTER.sub("___", inhalt.template)
        gefuellt = _PLATZHALTER.sub(lambda m: loesung.get(m.group(1), m.group(0)), inhalt.template)
        return (
            "open", {"text": gefuellt}, {"prompt": f"{inhalt.prompt}\n\n{luecken}"},
            [dict(_FALLBACK_CRITERION)],
        )
    blank = inhalt.blanks[0]
    antworten = [a for a in blank.accepted if a.strip()]
    darstellung: dict[str, Any] = {"template": inhalt.template, "shell": inhalt.shell}
    config: dict[str, Any] = {"prompt": inhalt.prompt}
    if inhalt.shell is not None:
        config["normalize"] = "command"
        darstellung.update(_fenster(inhalt, blank.blank_id, antworten[0]))
    config["presentation"] = darstellung
    return "fill_in", {"answers": antworten}, config, []


def _fenster(inhalt: Luecke, blank_id: str, antwort: str) -> dict[str, Any]:
    """The drawn window with its empty box, like the factory's image cloze; text only without
    the media folder or when the blank cannot be placed."""
    if inhalt.shell is None or not _media_available():
        return {}
    try:
        fenster = render_terminal(inhalt.shell, inhalt.template, blanks={blank_id: antwort})
        if blank_id not in fenster.rects:
            return {}
        felder: dict[str, Any] = {
            "image": save_media(fenster.png), "width": fenster.width, "height": fenster.height,
            "rect": fenster.rects[blank_id].model_dump(),
        }
    except (OSError, ValueError):
        logger.exception("werkstatt_katalog_fenster_fehlgeschlagen")
        return {}
    weitere = fenster.more.get(blank_id, ())
    if weitere:
        felder["more"] = [r.model_dump() for r in weitere]
    return felder
