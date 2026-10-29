"""Uebungskatalog je Freigabe: Items aus den Werkstatt-Inhalten, Lernstand bleibt bei unveraenderten Aufgaben."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ConceptMastery,
    ItemConcept,
    LearningEvaluation,
    Misconception,
    ReviewSchedule,
)
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    AnswerCommand,
    SessionStartCommand,
    SessionTurn,
)
from spaces.learning.services.db.models import Base
from spaces.learning.services.werkstatt.freigabe import (
    freigabe_ausfuehren,
    freigabe_vorbereiten,
    loesung_hash,
)
from spaces.learning.services.werkstatt.katalog import WerkstattKatalog
from spaces.learning.services.werkstatt.store import WerkstattStore
from spaces.learning.tests.unit.test_werkstatt_freigabe_ausfuehren import FakeGateway

COURSE = "6d0f4b8e-3a51-4c1e-9f0a-2b7c1d9e4a10"
LERNENDE = "lernende-a"


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'katalog.db'}")

    @event.listens_for(engine, "connect")
    def _fremdschluessel(connection, connection_record) -> None:
        del connection_record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _quiz(frage: str, konzept: str, richtig: list[str] | None = None) -> dict[str, Any]:
    return {
        "kind": "quiz", "prompt": frage, "concept_ids": [konzept],
        "options": [
            {"option_id": "a", "text": "ls"}, {"option_id": "b", "text": "cd"},
            {"option_id": "c", "text": "pwd"},
        ],
        "correct_option_ids": richtig or ["a"],
    }


def _offen(kind: str = "open") -> dict[str, Any]:
    return {
        "kind": kind, "prompt": "Erklären Sie, was ein Ordner ist.", "concept_ids": ["k2"],
        "expected_answer": "Ein Ordner fasst Dateien zusammen.",
        "rubric": [
            {"criterion_id": "kern", "description": "Nennt das Zusammenfassen", "points": 3},
            {"criterion_id": "beispiel", "description": "Gibt ein Beispiel", "points": 1},
        ],
    }


def _luecke(shell: str | None = "powershell") -> dict[str, Any]:
    return {
        "kind": "cloze", "prompt": "Ergänzen Sie den Befehl, der den Ordner wechselt.",
        "concept_ids": ["k3"], "template": "{{befehl}} Dokumente",
        "blanks": [{"blank_id": "befehl", "accepted": ["cd", "Set-Location"]}], "shell": shell,
    }


def _eintrag(key: str, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": key, "fassung": 1, "aufgaben_key": key,
        "loesung_hash": loesung_hash(content), "content": content,
    }


def _zwei_quiz(frage_a: str = "Welcher Befehl listet Dateien auf?", richtig_b: str = "b"):
    return [
        _eintrag("aufgabe-a", _quiz(frage_a, "k1")),
        _eintrag("aufgabe-b", _quiz("Welcher Befehl wechselt den Ordner?", "k2", [richtig_b])),
    ]


def _konzept_id(factory, revision: int, key: str) -> str:
    with factory() as s:
        return s.scalar(select(AdaptiveConcept.id).where(
            AdaptiveConcept.course_id == COURSE, AdaptiveConcept.course_revision == revision,
            AdaptiveConcept.concept_key == key,
        ))


def _richtig(factory, turn: SessionTurn) -> dict[str, Any]:
    with factory() as s:
        return dict(s.get(AdaptiveItem, turn.task.id).expected_answer)


def _start(service: AdaptiveSessionService, revision: int, versuche: int = 2) -> SessionTurn:
    return service.start(SessionStartCommand(
        course_id=COURSE, course_revision=revision, actor_id=LERNENDE, mode="training",
        blueprint={"max_attempts": versuche},
    ))


def _antworten(factory, service: AdaptiveSessionService, turn: SessionTurn) -> SessionTurn:
    return service.answer(AnswerCommand(
        session_id=turn.session_id, actor_id=LERNENDE,
        expected_session_revision=turn.session_revision, answer=_richtig(factory, turn),
    ))


def _lernstand_auf_revision_1(factory) -> None:
    """Both quiz tasks answered once on revision 1, plus a misconception on k1."""
    WerkstattKatalog(factory).projizieren(COURSE, 1, _zwei_quiz(), None)
    service = AdaptiveSessionService(factory)
    turn = _start(service, 1)
    turn = _antworten(factory, service, turn)
    _antworten(factory, service, turn)
    with factory() as s, s.begin():
        bewertung = s.scalars(select(LearningEvaluation.id)).first()
        s.add(Misconception(
            id="7a1e2c3d-0000-4000-8000-000000000001", actor_id=LERNENDE,
            concept_id=_konzept_id(factory, 1, "k1"), tag="verwechselt-ls-cd", status="unresolved",
            first_evaluation_id=bewertung, latest_evaluation_id=bewertung,
        ))


def _zeilen(factory, modell, konzept_id: str) -> list[Any]:
    with factory() as s:
        return list(s.scalars(select(modell).where(modell.concept_id == konzept_id)))


def _anzahl(factory) -> dict[str, int]:
    with factory() as s:
        return {
            m.__tablename__: s.scalar(select(func.count()).select_from(m))
            for m in (AdaptiveConcept, AdaptiveItem, ItemConcept, ConceptMastery, ReviewSchedule, Misconception)
        }


def test_jede_aufgabe_wird_ein_item_ihres_typs(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path / "artefakte"))
    factory = _factory(tmp_path)
    mehrere = _quiz("Welche Befehle zeigen etwas an?", "k1", ["a", "c"])
    mehrfach_luecke = {
        "kind": "cloze", "prompt": "Ergänzen Sie den Satz.", "concept_ids": ["k3"],
        "template": "Mit {{a}} wechseln Sie, mit {{b}} listen Sie.",
        "blanks": [{"blank_id": "a", "accepted": ["cd"]}, {"blank_id": "b", "accepted": ["ls"]}],
    }
    reihenfolge = {
        "kind": "ordering", "prompt": "Bringen Sie die Schritte in die richtige Reihenfolge.",
        "concept_ids": ["k4"], "items": ["Terminal öffnen", "Befehl tippen", "Enter drücken"],
    }
    zuordnung = {
        "kind": "matching", "prompt": "Ordnen Sie jedem Befehl seine Wirkung zu.", "concept_ids": ["k4"],
        "pairs": [
            {"left": "ls", "right": "listet Dateien"}, {"left": "cd", "right": "wechselt den Ordner"},
            {"left": "pwd", "right": "zeigt den Ordner"},
        ],
    }
    eintraege = [
        _eintrag("quiz", _quiz("Welcher Befehl listet Dateien auf?", "k1")),
        _eintrag("quiz-mehrere", mehrere),
        _eintrag("offen", _offen()),
        _eintrag("fall", _offen("case")),
        _eintrag("luecke-text", _luecke(None)),
        _eintrag("luecke-fenster", _luecke("powershell")),
        _eintrag("luecke-mehrfach", mehrfach_luecke),
        _eintrag("reihenfolge", reihenfolge),
        _eintrag("zuordnung", zuordnung),
    ]

    ergebnis = WerkstattKatalog(factory).projizieren(COURSE, 3, eintraege, None)

    assert ergebnis == {"items": 9, "konzepte": 4, "lernstand_uebernommen": 0}
    with factory() as s:
        items = {i.activity_id: i for i in s.scalars(select(AdaptiveItem))}
        konzepte = {c.concept_key: c for c in s.scalars(select(AdaptiveConcept))}
        links = {(l.item_id, l.concept_id) for l in s.scalars(select(ItemConcept))}
    assert set(konzepte) == {"k1", "k2", "k3", "k4"}
    assert all(c.course_revision == 3 and c.course_id == COURSE for c in konzepte.values())
    assert all(i.course_revision == 3 and i.quality_status == "approved" for i in items.values())
    assert (items["quiz"].id, konzepte["k1"].id) in links
    assert (items["zuordnung"].id, konzepte["k4"].id) in links and len(links) == 9

    quiz = items["quiz"]
    assert quiz.item_type == "single_choice" and quiz.expected_answer == {"choice_id": "a"}
    assert sorted(o["id"] for o in quiz.scoring_config["options"]) == ["a", "b", "c"]
    assert quiz.scoring_config["prompt"] == "Welcher Befehl listet Dateien auf?" and quiz.rubric == []
    assert items["quiz-mehrere"].item_type == "multiple_choice"
    assert items["quiz-mehrere"].expected_answer == {"choice_ids": ["a", "c"]}

    offen, fall = items["offen"], items["fall"]
    assert (offen.item_type, fall.item_type) == ("open", "case")
    assert offen.expected_answer == {"text": "Ein Ordner fasst Dateien zusammen."}
    assert [k["points"] for k in offen.rubric] == [75, 25]

    text = items["luecke-text"]
    assert text.item_type == "fill_in" and text.expected_answer == {"answers": ["cd", "Set-Location"]}
    assert text.scoring_config["presentation"] == {"template": "{{befehl}} Dokumente", "shell": None}
    fenster = items["luecke-fenster"].scoring_config
    assert fenster["normalize"] == "command"
    darstellung = fenster["presentation"]
    assert darstellung["template"] == "{{befehl}} Dokumente" and darstellung["shell"] == "powershell"
    assert darstellung["image"].startswith("/lernsequenz-medien/term-")
    assert darstellung["width"] > 0 and darstellung["height"] > 0 and set(darstellung["rect"]) == {"x", "y", "w", "h"}

    # Several blanks: the engine scores one text per fill_in, so it becomes an open answer.
    mehrfach = items["luecke-mehrfach"]
    assert mehrfach.item_type == "open"
    assert mehrfach.expected_answer == {"text": "Mit cd wechseln Sie, mit ls listen Sie."}
    assert "Mit ___ wechseln Sie, mit ___ listen Sie." in mehrfach.scoring_config["prompt"]

    ordnung = items["reihenfolge"]
    assert ordnung.item_type == "ordering"
    beschriftung = {o["id"]: o["label"] for o in ordnung.scoring_config["options"]}
    assert [beschriftung[i] for i in ordnung.expected_answer["order"]] == reihenfolge["items"]
    assert ordnung.scoring_config["partial_credit"] is True

    paare = items["zuordnung"]
    assert paare.item_type == "matching"
    links_seite = {e["id"]: e["label"] for e in paare.scoring_config["presentation"]["left"]}
    rechts_seite = {e["id"]: e["label"] for e in paare.scoring_config["presentation"]["right"]}
    assert {links_seite[l]: rechts_seite[r] for l, r in paare.expected_answer["pairs"].items()} == {
        p["left"]: p["right"] for p in zuordnung["pairs"]
    }

    # "Üben" serves them: the deterministic ones start without a model.
    turn = _start(AdaptiveSessionService(factory), 3)
    assert turn.task is not None


def test_lernstand_bleibt_bei_unveraenderten_aufgaben(tmp_path):
    factory = _factory(tmp_path)
    _lernstand_auf_revision_1(factory)
    alt = _konzept_id(factory, 1, "k1")
    (alte_mastery,) = _zeilen(factory, ConceptMastery, alt)

    # Task b gets a new solution; task a stays as it was.
    ergebnis = WerkstattKatalog(factory).projizieren(COURSE, 2, _zwei_quiz(richtig_b="c"), 1,
                                                     vorher_eintraege=_zwei_quiz())

    neu = _konzept_id(factory, 2, "k1")
    assert neu != alt
    (mastery,) = _zeilen(factory, ConceptMastery, neu)
    assert (mastery.actor_id, mastery.alpha, mastery.beta) == (LERNENDE, alte_mastery.alpha, alte_mastery.beta)
    (plan,) = _zeilen(factory, ReviewSchedule, neu)
    (alter_plan,) = _zeilen(factory, ReviewSchedule, alt)
    assert (plan.interval_index, plan.last_evaluation_id) == (alter_plan.interval_index, alter_plan.last_evaluation_id)
    (fehlvorstellung,) = _zeilen(factory, Misconception, neu)
    assert (fehlvorstellung.tag, fehlvorstellung.status) == ("verwechselt-ls-cd", "unresolved")
    # The old rows stay: a session on revision 1 keeps its state.
    assert len(_zeilen(factory, ConceptMastery, alt)) == 1 and len(_zeilen(factory, Misconception, alt)) == 1
    assert ergebnis == {"items": 2, "konzepte": 2, "lernstand_uebernommen": 1}


def test_lernstand_neu_bei_geaenderter_loesung(tmp_path):
    factory = _factory(tmp_path)
    _lernstand_auf_revision_1(factory)
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 1, "k2"))

    WerkstattKatalog(factory).projizieren(COURSE, 2, _zwei_quiz(richtig_b="c"), 1,
                                          vorher_eintraege=_zwei_quiz())

    neu = _konzept_id(factory, 2, "k2")
    assert _zeilen(factory, ConceptMastery, neu) == []
    assert _zeilen(factory, ReviewSchedule, neu) == []


def test_textkorrektur_behaelt_lernstand(tmp_path):
    factory = _factory(tmp_path)
    _lernstand_auf_revision_1(factory)

    # Only the wording of both questions changed (same solution, kind and concepts).
    neu = _zwei_quiz(frage_a="Mit welchem Befehl listen Sie Dateien auf?")
    neu[1]["content"]["prompt"] = "Mit welchem Befehl wechseln Sie den Ordner?"
    neu[1]["loesung_hash"] = loesung_hash(neu[1]["content"])
    assert [e["loesung_hash"] for e in neu] == [e["loesung_hash"] for e in _zwei_quiz()]
    ergebnis = WerkstattKatalog(factory).projizieren(COURSE, 2, neu, 1, vorher_eintraege=_zwei_quiz())

    assert ergebnis["lernstand_uebernommen"] == 2
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 2, "k1"))
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 2, "k2"))


def test_projektion_ist_idempotent(tmp_path):
    factory = _factory(tmp_path)
    _lernstand_auf_revision_1(factory)
    katalog = WerkstattKatalog(factory)

    katalog.projizieren(COURSE, 2, _zwei_quiz(), 1, vorher_eintraege=_zwei_quiz())
    vorher = _anzahl(factory)
    zweites = katalog.projizieren(COURSE, 2, _zwei_quiz(), 1, vorher_eintraege=_zwei_quiz())

    assert _anzahl(factory) == vorher
    assert zweites == {"items": 0, "konzepte": 0, "lernstand_uebernommen": 0}


def test_alte_sitzung_bleibt_nach_freigabe_nutzbar(tmp_path):
    factory = _factory(tmp_path)
    katalog = WerkstattKatalog(factory)
    katalog.projizieren(COURSE, 1, _zwei_quiz(), None)
    service = AdaptiveSessionService(factory)
    with factory() as s:
        alte_items = set(s.scalars(select(AdaptiveItem.id).where(AdaptiveItem.course_revision == 1)))
    laufend = _start(service, 1, versuche=3)
    laufend = _antworten(factory, service, laufend)

    katalog.projizieren(COURSE, 2, _zwei_quiz(richtig_b="c"), 1, vorher_eintraege=_zwei_quiz())

    # The running session keeps serving revision 1 ...
    assert service.current(laufend.session_id, actor_id=LERNENDE).task.id in alte_items
    weiter = _antworten(factory, service, laufend)
    assert weiter.task is not None and weiter.task.id in alte_items
    # ... and a new session gets revision 2.
    with factory() as s:
        neue_items = set(s.scalars(select(AdaptiveItem.id).where(AdaptiveItem.course_revision == 2)))
    neu = service.start(SessionStartCommand(
        course_id=COURSE, course_revision=2, actor_id="lernende-b", mode="training",
        blueprint={"max_attempts": 1},
    ))
    assert neu.task is not None and neu.task.id in neue_items


# --- Hook in freigabe_ausfuehren -----------------------------------------------------


def _store(factory) -> WerkstattStore:
    s = WerkstattStore(factory)
    s.kurs_anlegen(
        COURSE, title="Terminal", audience="Einsteiger", default_kind="quiz",
        outcomes=["Befehle eingeben"], source_provenance=[], owner_ref="kursleitung-a",
    )
    return s


def _aufgabe(s: WerkstattStore, content: dict[str, Any], art: str = "neu") -> str:
    t = s.aufgabe_anlegen(COURSE, chapter_title="Kapitel 1", summary=content["prompt"][:40])
    s.fassung_anlegen(t["task_id"], content=content, based_on=None, author_ref="kursleitung-a",
                      reason="r", art=art, findings=[])
    return t["task_id"]


def _fassung(s: WerkstattStore, task_id: str, content: dict[str, Any], art: str) -> None:
    s.fassung_anlegen(task_id, content=content, based_on=s.neueste(task_id)["number"],
                      author_ref="kursleitung-a", reason="r", art=art, findings=[])


async def _freigeben(s: WerkstattStore, katalog: Any, revision: int) -> dict[str, Any]:
    v = freigabe_vorbereiten(s, COURSE, autor="kursleitung-a", learnhouse_revision=revision)
    return await freigabe_ausfuehren(
        s, FakeGateway(), v["freigabe_id"], approval_ref="klick", learnhouse_revision=revision,
        katalog=katalog,
    )


def _lernen(factory, revision: int) -> None:
    service = AdaptiveSessionService(factory)
    turn = _start(service, revision)
    turn = _antworten(factory, service, turn)
    _antworten(factory, service, turn)


@pytest.mark.asyncio
async def test_freigabe_projiziert_katalog_der_neuen_revision(tmp_path):
    factory = _factory(tmp_path)
    s = _store(factory)
    katalog = WerkstattKatalog(factory)
    t1 = _aufgabe(s, _quiz("Welcher Befehl listet Dateien auf?", "k1"))
    t2 = _aufgabe(s, _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["b"]))

    erste = await _freigeben(s, katalog, 4)

    assert erste["learnhouse_revision"] == 5
    assert erste["katalog"] == {"items": 2, "konzepte": 2, "lernstand_uebernommen": 0}
    with factory() as session:
        keys = set(session.scalars(select(AdaptiveItem.activity_id).where(AdaptiveItem.course_revision == 5)))
    assert keys == {f"werkstatt:{t1}", f"werkstatt:{t2}"}

    _lernen(factory, 5)
    _fassung(s, t1, _quiz("Mit welchem Befehl listen Sie Dateien auf?", "k1"), "textkorrektur")
    _fassung(s, t2, _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["c"]), "neu")
    zweite = await _freigeben(s, katalog, 5)

    assert zweite["katalog"] == {"items": 2, "konzepte": 2, "lernstand_uebernommen": 1}
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 6, "k1"))
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 6, "k2")) == []


@pytest.mark.asyncio
async def test_erste_freigabe_nach_uebernahme_behaelt_lernstand(tmp_path):
    # The taken-over course already had a catalog (revision 2) and learners on it; its tasks
    # came in as Fassung 1 ("uebernommen"), which is what that catalog held.
    factory = _factory(tmp_path)
    s = _store(factory)
    katalog = WerkstattKatalog(factory)
    a, b = _quiz("Welcher Befehl listet Dateien auf?", "k1"), _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["b"])
    katalog.projizieren(COURSE, 2, [_eintrag("fabrik-a", a), _eintrag("fabrik-b", b)], None)
    _lernen(factory, 2)
    t1, t2 = _aufgabe(s, a, art="uebernommen"), _aufgabe(s, b, art="uebernommen")
    s.herkunft_setzen({t1: ("fabrik-a", None), t2: ("fabrik-b", None)})  # as the takeover keeps them
    _fassung(s, t1, _quiz("Mit welchem Befehl listen Sie Dateien auf?", "k1"), "textkorrektur")
    _fassung(s, t2, _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["c"]), "neu")

    r = await _freigeben(s, katalog, 2)

    assert r["katalog"]["lernstand_uebernommen"] == 1
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 3, "k1"))
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 3, "k2")) == []


class _KaputterKatalog:
    def letzte_revision(self, course_id: str, *, unter: int) -> int | None:
        return None

    def projizieren(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("datenbank weg")


@pytest.mark.asyncio
async def test_katalog_fehler_haelt_freigabe_nicht_auf(tmp_path, caplog):
    factory = _factory(tmp_path)
    s = _store(factory)
    _aufgabe(s, _quiz("Welcher Befehl listet Dateien auf?", "k1"))

    with caplog.at_level(logging.ERROR):
        r = await _freigeben(s, _KaputterKatalog(), 1)

    assert r["katalog_fehler"] == "RuntimeError" and "katalog" not in r
    assert r["learnhouse_revision"] == 2
    assert s.freigabe_letzte_freigegebene(COURSE)["zustand"] == "freigegeben"
    assert any(rec.message == "werkstatt_katalog_projektion_fehlgeschlagen" for rec in caplog.records)


# --- Mischen: die gezeigte Reihenfolge verraet die Loesung nicht -----------------------

_SCHRITTE = ["Terminal öffnen", "Ordner wechseln", "Befehl tippen", "Enter drücken"]


def _reihenfolge() -> dict[str, Any]:
    return {
        "kind": "ordering", "prompt": "Bringen Sie die Schritte in die richtige Reihenfolge.",
        "concept_ids": ["k4"], "items": list(_SCHRITTE),
    }


def _zuordnung() -> dict[str, Any]:
    return {
        "kind": "matching", "prompt": "Ordnen Sie jedem Befehl seine Wirkung zu.", "concept_ids": ["k4"],
        "pairs": [
            {"left": "ls", "right": "listet Dateien"}, {"left": "cd", "right": "wechselt den Ordner"},
            {"left": "pwd", "right": "zeigt den Ordner"}, {"left": "mkdir", "right": "legt einen Ordner an"},
        ],
    }


class _Zufall:
    """Stands in for the secure RNG: the first `identisch` shuffles keep the order, then it reverses."""

    def __init__(self, identisch: int = 0) -> None:
        self.aufrufe = 0
        self.identisch = identisch

    def shuffle(self, liste: list[Any]) -> None:
        self.aufrufe += 1
        if self.aufrufe > self.identisch:
            liste.reverse()


def _gezeigt(factory, revision: int, key: str) -> tuple[AdaptiveItem, list[str]]:
    with factory() as s:
        item = s.scalar(select(AdaptiveItem).where(
            AdaptiveItem.course_revision == revision, AdaptiveItem.activity_id == key,
        ))
    if item.item_type == "ordering":
        return item, [o["label"] for o in item.scoring_config["options"]]
    return item, [e["label"] for e in item.scoring_config["presentation"]["right"]]


def test_reihenfolge_nicht_aus_item_id_ableitbar(tmp_path, monkeypatch):
    import random

    from spaces.learning.services.werkstatt import katalog as katalog_modul

    zufall = _Zufall()
    monkeypatch.setattr(katalog_modul, "_ZUFALL", zufall)
    factory = _factory(tmp_path)
    WerkstattKatalog(factory).projizieren(
        COURSE, 1, [_eintrag("reihenfolge", _reihenfolge()), _eintrag("zuordnung", _zuordnung())], None
    )

    for key, autor in (("reihenfolge", _SCHRITTE), ("zuordnung", [p["right"] for p in _zuordnung()["pairs"]])):
        item, gezeigt = _gezeigt(factory, 1, key)
        # The shown order comes from the secure RNG, not from a seed anyone can recompute.
        assert gezeigt == list(reversed(autor))
        aus_id = list(autor)
        random.Random(item.id).shuffle(aus_id)
        assert aus_id != gezeigt
    assert zufall.aufrufe == 2


def test_gemischte_reihenfolge_ist_nie_die_loesung(tmp_path, monkeypatch):
    from spaces.learning.services.werkstatt import katalog as katalog_modul

    # The RNG happens to keep the authored order twice: it is shuffled again until it differs.
    zufall = _Zufall(identisch=2)
    monkeypatch.setattr(katalog_modul, "_ZUFALL", zufall)
    factory = _factory(tmp_path)
    WerkstattKatalog(factory).projizieren(COURSE, 1, [_eintrag("reihenfolge", _reihenfolge())], None)

    item, gezeigt = _gezeigt(factory, 1, "reihenfolge")
    loesung = {o["id"]: o["label"] for o in item.scoring_config["options"]}
    assert gezeigt != [loesung[i] for i in item.expected_answer["order"]]
    assert zufall.aufrufe == 3

    # With the real RNG, never the solution either (matching: right side in authored order).
    monkeypatch.undo()
    for revision in range(2, 12):
        WerkstattKatalog(factory).projizieren(
            COURSE, revision, [_eintrag("reihenfolge", _reihenfolge()), _eintrag("zuordnung", _zuordnung())], None
        )
        assert _gezeigt(factory, revision, "reihenfolge")[1] != _SCHRITTE
        assert _gezeigt(factory, revision, "zuordnung")[1] != [p["right"] for p in _zuordnung()["pairs"]]


# --- Fehlgeschlagene Projektion: Lernstand geht nicht verloren, Katalog nachholbar -------


def _hat_katalog(factory, revision: int) -> bool:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(AdaptiveConcept).where(
            AdaptiveConcept.course_id == COURSE, AdaptiveConcept.course_revision == revision,
        )) > 0


@pytest.mark.asyncio
async def test_fehlgeschlagene_projektion_verliert_keinen_lernstand(tmp_path):
    # Taken-over course: catalog revision 2 with progress. Release 1 (revision 3) loses its
    # catalog; release 2 (revision 4) must still carry over from revision 2.
    factory = _factory(tmp_path)
    s = _store(factory)
    katalog = WerkstattKatalog(factory)
    a, b = _quiz("Welcher Befehl listet Dateien auf?", "k1"), _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["b"])
    katalog.projizieren(COURSE, 2, [_eintrag("fabrik-a", a), _eintrag("fabrik-b", b)], None)
    _lernen(factory, 2)
    t1, t2 = _aufgabe(s, a, art="uebernommen"), _aufgabe(s, b, art="uebernommen")
    s.herkunft_setzen({t1: ("fabrik-a", None), t2: ("fabrik-b", None)})  # as the takeover keeps them

    _fassung(s, t1, _quiz("Mit welchem Befehl listen Sie Dateien auf?", "k1"), "textkorrektur")
    erste = await _freigeben(s, _KaputterKatalog(), 2)
    assert erste["katalog_fehler"] == "RuntimeError" and not _hat_katalog(factory, 3)

    _fassung(s, t2, _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["c"]), "neu")
    zweite = await _freigeben(s, katalog, 3)

    assert zweite["katalog"]["lernstand_uebernommen"] == 1
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 4, "k1"))
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 4, "k2")) == []


@pytest.mark.asyncio
async def test_vorher_ist_letzter_katalog_mit_seiner_freigabe(tmp_path):
    # Release 1 has a catalog (revision 5), release 2 not (revision 6): release 3 compares with
    # release 1's manifest, since revision 5 is what learners practised on.
    factory = _factory(tmp_path)
    s = _store(factory)
    katalog = WerkstattKatalog(factory)
    t1 = _aufgabe(s, _quiz("Welcher Befehl listet Dateien auf?", "k1"))
    t2 = _aufgabe(s, _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["b"]))
    await _freigeben(s, katalog, 4)
    _lernen(factory, 5)
    _fassung(s, t1, _quiz("Mit welchem Befehl listen Sie Dateien auf?", "k1"), "textkorrektur")
    assert "katalog_fehler" in await _freigeben(s, _KaputterKatalog(), 5)

    _fassung(s, t2, _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["c"]), "neu")
    dritte = await _freigeben(s, katalog, 6)

    assert dritte["katalog"]["lernstand_uebernommen"] == 1
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 7, "k1"))
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 7, "k2")) == []


@pytest.mark.asyncio
async def test_katalog_nachholen_projiziert_fehlende_revision(tmp_path):
    from spaces.learning.services.werkstatt.freigabe import katalog_nachholen

    factory = _factory(tmp_path)
    s = _store(factory)
    katalog = WerkstattKatalog(factory)
    t1 = _aufgabe(s, _quiz("Welcher Befehl listet Dateien auf?", "k1"))
    _aufgabe(s, _quiz("Welcher Befehl wechselt den Ordner?", "k2", ["b"]))
    await _freigeben(s, katalog, 4)
    _lernen(factory, 5)
    _fassung(s, t1, _quiz("Mit welchem Befehl listen Sie Dateien auf?", "k1"), "textkorrektur")
    zweite = await _freigeben(s, _KaputterKatalog(), 5)
    assert "katalog_fehler" in zweite and not _hat_katalog(factory, 6)

    r = katalog_nachholen(s, katalog, COURSE)

    assert r["learnhouse_revision"] == 6
    assert r["freigabe_id"] == s.freigabe_letzte_freigegebene(COURSE)["id"]
    assert r["katalog"] == {"items": 2, "konzepte": 2, "lernstand_uebernommen": 2}
    assert _zeilen(factory, ConceptMastery, _konzept_id(factory, 6, "k1"))
    # Nothing left to catch up.
    assert katalog_nachholen(s, katalog, COURSE) == {"nachgeholt": False}


# --- Zahl-Aufgabe und Formelsatz im Mathe-Kurs ----------------------------------------


def _zahl(label: str | None = "x =") -> dict[str, Any]:
    inhalt: dict[str, Any] = {
        "kind": "number", "prompt": "Berechnen Sie die Minimalstelle von $f(x)=x^3-3x$.",
        "concept_ids": ["k5"], "value": 1.0, "tolerance": 0.05,
        "rechnung": "solve(diff(x**3-3*x, x), x)[1]",
    }
    if label is not None:
        inhalt["label"] = label
    return inhalt


def test_zahl_wird_numeric_item() -> None:
    from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
    from spaces.learning.services.werkstatt.katalog import _item_felder

    typ, erwartet, config, rubrik = _item_felder("item-1", inhalt_lesen(_zahl()))
    assert (typ, erwartet, rubrik) == ("numeric", {"value": 1.0}, [])
    assert config == {
        "prompt": "Berechnen Sie die Minimalstelle von $f(x)=x^3-3x$.",
        "absolute_tolerance": 0.05, "presentation": {"unit": "x ="},
    }
    _typ, _erwartet, ohne, _rubrik = _item_felder("item-1", inhalt_lesen(_zahl(label=None)))
    assert ohne["presentation"] == {}


def _items(factory, revision: int) -> dict[str, AdaptiveItem]:
    with factory() as s:
        return {
            i.activity_id: i
            for i in s.scalars(select(AdaptiveItem).where(AdaptiveItem.course_revision == revision))
        }


def test_mathe_kurs_setzt_formelsatz_auf_jedem_item(tmp_path):
    factory = _factory(tmp_path)
    eintraege = [_eintrag("quiz", _quiz("Welcher Wert ist $f(1)$?", "k1")), _eintrag("zahl", _zahl())]

    WerkstattKatalog(factory).projizieren(COURSE, 3, eintraege, None, fach="mathe")
    WerkstattKatalog(factory).projizieren(COURSE, 4, eintraege, None)

    mathe, terminal = _items(factory, 3), _items(factory, 4)
    assert mathe["quiz"].scoring_config["presentation"] == {"math": True}
    assert mathe["zahl"].scoring_config["presentation"] == {"unit": "x =", "math": True}
    assert mathe["zahl"].item_type == "numeric" and mathe["zahl"].expected_answer == {"value": 1.0}
    assert "math" not in terminal["quiz"].scoring_config["presentation"]
    assert terminal["zahl"].scoring_config["presentation"] == {"unit": "x ="}

    # The served task carries the switch (and the unit), never the solution.
    service = AdaptiveSessionService(factory)
    turn = _start(service, 3)
    gesehen = {}
    while turn.task is not None and turn.task.id not in gesehen:
        gesehen[turn.task.id] = dict(turn.task.presentation)
        turn = _antworten(factory, service, turn)
    nach_typ = {mathe[k].id: k for k in mathe}
    assert {nach_typ[i]: p for i, p in gesehen.items()} == {
        "quiz": {"math": True}, "zahl": {"unit": "x =", "math": True},
    }


@pytest.mark.asyncio
async def test_freigabe_reicht_fach_des_kurses_durch(tmp_path):
    factory = _factory(tmp_path)
    s = WerkstattStore(factory)
    s.kurs_anlegen(
        COURSE, title="Kurvendiskussion", audience="Oberstufe", default_kind="number",
        outcomes=["Extrema bestimmen"], source_provenance=[], owner_ref="kursleitung-a", fach="mathe",
    )
    t = _aufgabe(s, _zahl())

    r = await _freigeben(s, WerkstattKatalog(factory), 1)

    assert r["katalog"]["items"] == 1
    (item,) = _items(factory, 2).values()
    assert item.activity_id == f"werkstatt:{t}"
    assert item.scoring_config["presentation"] == {"unit": "x =", "math": True}


# --- Graph-Bilder in Zuordnungen ------------------------------------------------------


def _graph_zuordnung() -> dict[str, Any]:
    return {
        "kind": "matching", "prompt": "Ordnen Sie jedem Graphen den Graphen seiner Ableitung zu.",
        "concept_ids": ["k6"], "beziehung": "ableitung_von",
        "pairs": [
            {"left": "f(x)=x^3", "right": "3x^2", "left_graph": {"term": "x**3"}, "right_graph": {"term": "3*x**2"}},
            {"left": "g", "right": "g'", "left_graph": {"term": "sin(x)"}, "right_graph": {"term": "cos(x)"}},
            {"left": "h", "right": "h'", "left_graph": {"term": "x**2-1", "y_min": -2}, "right_graph": {"term": "2*x"}},
        ],
    }


def test_zuordnung_mit_graphen_zeigt_bilder_statt_terme(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path / "artefakte"))
    factory = _factory(tmp_path)
    inhalt = _graph_zuordnung()

    WerkstattKatalog(factory).projizieren(COURSE, 5, [_eintrag("graphen", inhalt)], None, fach="mathe")

    (item,) = _items(factory, 5).values()
    darstellung = item.scoring_config["presentation"]
    assert item.item_type == "matching" and darstellung["math"] is True
    assert [e["label"] for e in darstellung["left"]] == ["Graph A", "Graph B", "Graph C"]
    assert sorted(e["label"] for e in darstellung["right"]) == ["Graph 1", "Graph 2", "Graph 3"]
    medien = tmp_path / "artefakte" / "lernsequenzen" / "medien"
    for e in darstellung["left"] + darstellung["right"]:
        assert set(e) == {"id", "label", "image", "width", "height"}
        assert e["image"].startswith("/lernsequenz-medien/term-") and (e["width"], e["height"]) == (480, 360)
        assert (medien / e["image"].rsplit("/", 1)[1]).is_file()
    assert len({e["image"] for e in darstellung["left"] + darstellung["right"]}) == 6
    # The solution still pairs the drawn graphs as authored.
    bild = {e["id"]: e["image"] for e in darstellung["left"] + darstellung["right"]}
    from spaces.learning.services.werkstatt.graph import graph_zeichnen
    from spaces.learning.services.werkstatt.inhalt import GraphAngabe
    from spaces.learning.services.lernsequenz.medien import save_media

    erwartet = {
        save_media(graph_zeichnen(GraphAngabe(**p["left_graph"])).png):
            save_media(graph_zeichnen(GraphAngabe(**p["right_graph"])).png)
        for p in inhalt["pairs"]
    }
    assert {bild[l]: bild[r] for l, r in item.expected_answer["pairs"].items()} == erwartet

    # No term (nor the author's side texts) anywhere in what is stored for the learner.
    gespeichert = json.dumps([item.scoring_config, item.expected_answer], ensure_ascii=False)
    for text in ("x**3", "3*x**2", "sin", "cos", "x^3", "3x^2", "f(x)", "g'"):
        assert text not in gespeichert

    turn = _start(AdaptiveSessionService(factory), 5)
    gezeigt = json.dumps(turn.task.presentation, ensure_ascii=False)
    assert "x**3" not in gezeigt and "Graph A" in gezeigt and "/lernsequenz-medien/term-" in gezeigt


def test_zuordnung_mit_graphen_ohne_medienordner_nur_etikett(tmp_path, monkeypatch):
    monkeypatch.delenv("LEARNING_ARTIFACT_ROOT", raising=False)
    from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
    from spaces.learning.services.werkstatt.katalog import _item_felder

    _typ, _erwartet, config, _rubrik = _item_felder("item-1", inhalt_lesen(_graph_zuordnung()))
    seiten = config["presentation"]["left"] + config["presentation"]["right"]
    assert all(set(e) == {"id", "label"} for e in seiten)
    assert "x**3" not in str(config)


def test_zuordnung_gemischt_text_und_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path / "artefakte"))
    from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
    from spaces.learning.services.werkstatt.katalog import _item_felder

    inhalt = _graph_zuordnung()
    del inhalt["beziehung"]
    for p in inhalt["pairs"]:
        del p["right_graph"]
    _typ, erwartet, config, _rubrik = _item_felder("item-1", inhalt_lesen(inhalt))
    links, rechts = config["presentation"]["left"], config["presentation"]["right"]
    assert [e["label"] for e in links] == ["Graph A", "Graph B", "Graph C"]
    assert sorted(e["label"] for e in rechts) == ["3x^2", "g'", "h'"]
    assert all("image" not in e for e in rechts)
    beschriftung = {e["id"]: e["label"] for e in links + rechts}
    assert {beschriftung[l]: beschriftung[r] for l, r in erwartet["pairs"].items()} == {
        "Graph A": "3x^2", "Graph B": "g'", "Graph C": "h'",
    }
