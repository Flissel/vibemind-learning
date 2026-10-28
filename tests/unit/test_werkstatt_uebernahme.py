"""Takeover of a published course as Fassung 1 (fake artifact store, no real course data)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.course_factory.models import CourseFactoryPublication
from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.db.models import Base
from spaces.learning.services.werkstatt.models import WerkstattAufgabe, WerkstattFassung
from spaces.learning.services.werkstatt.store import WerkstattStore
from spaces.learning.services.werkstatt.uebernahme import kurs_uebernehmen

COURSE = "11111111-1111-4111-8111-111111111111"
JOB = "22222222-2222-4222-8222-222222222222"
SRC = "33333333-3333-4333-8333-333333333333"
HASH = "a" * 64


def _citation(n: int) -> dict[str, Any]:
    return {
        "citation_id": f"{n:036d}", "source_id": SRC, "source_revision": 1,
        "chunk_id": f"{n + 100:036d}", "content_hash": HASH, "locator": {"page": n},
    }


def _activity(aid: str, chapter: str, typ: str, cites: list[str]) -> dict[str, Any]:
    quiz = typ == "quiz"
    return {
        "activity_id": aid, "chapter_id": chapter, "type": typ, "concept_ids": ["k1"],
        "prompt": f"Frage {aid}: " + "x" * 200, "expected_answer": "" if quiz else "Antwort",
        "citation_ids": cites,
        "rubric": [] if quiz else [{"criterion_id": "r1", "description": "d", "points": 2}],
        "options": [{"option_id": "a", "text": "A"}, {"option_id": "b", "text": "B"}] if quiz else [],
        "correct_option_ids": ["a"] if quiz else [],
    }


def _draft(*, code: bool = False) -> dict[str, Any]:
    kap1 = [_activity("a1", "ch1", "quiz", [f"{1:036d}"]), _activity("a2", "ch1", "open", [])]
    kap2 = [_activity("a3", "ch2", "code" if code else "case", [f"{2:036d}"])]
    if code:
        kap2.append(_activity("a4", "ch2", "quiz", []))
    return {
        "schema_version": "course-draft-v1", "job_id": JOB, "attempt_number": 1,
        "title": "Beispielkurs", "outcomes": ["Ziel 1"],
        "source_provenance": [{"source_id": SRC, "revision": 1, "content_hash": HASH}],
        "citations": [_citation(1), _citation(2)],
        "concepts": [{"concept_id": "k1", "title": "K"}],
        "chapters": [
            {"chapter_id": "ch1", "title": "Kapitel Eins", "lesson_ids": ["l1", "l2"],
             "activity_ids": ["a1", "a2"]},
            {"chapter_id": "ch2", "title": "Kapitel Zwei", "lesson_ids": ["l3"],
             "activity_ids": [a["activity_id"] for a in kap2]},
        ],
        "lessons": [
            {"lesson_id": "l1", "chapter_id": "ch1", "title": "Lektion A", "body": "Text A",
             "concept_ids": ["k1"],
             "claims": [{"claim_id": "c1", "text": "Aussage", "citation_ids": [f"{1:036d}"]}]},
            {"lesson_id": "l2", "chapter_id": "ch1", "title": "Lektion B", "body": "Text B",
             "concept_ids": ["k1"], "claims": []},
            {"lesson_id": "l3", "chapter_id": "ch2", "title": "Lektion C", "body": "Text C",
             "concept_ids": ["k1"], "claims": []},
        ],
        "activities": kap1 + kap2,
    }


class _FakeArtifacts:
    def __init__(self, draft: dict[str, Any]) -> None:
        self.draft = draft
        self.calls: list[tuple[str, int, str]] = []

    def read_stage_output(self, job_id: str, *, attempt_number: int, stage: Any) -> dict[str, Any]:
        self.calls.append((job_id, attempt_number, stage.value))
        return {"source_verifier": {"schema_version": "x", "draft": self.draft,
                                    "unsupported_claim_ids": []}}


def _setup(tmp_path: Path, draft: dict[str, Any] | None = None, publiziert: bool = True):
    engine = create_engine(f"sqlite:///{tmp_path / 'u.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    if publiziert:
        with factory.begin() as session:
            session.add(CourseFactoryPublication(
                id=str(uuid4()), job_id=JOB, attempt_id=str(uuid4()), attempt_number=1,
                course_id=COURSE, draft_hash=HASH, learnhouse_revision=1,
                confirmation_ref="c", readback_evidence_ref="r",
            ))
    fake = _FakeArtifacts(draft or _draft())
    return WerkstattStore(factory), fake, factory


def _uebernehmen(store, fake, factory):
    return kurs_uebernehmen(store, fake, factory, COURSE, author_ref="anna")


def test_uebernahme_legt_je_aktivitaet_eine_aufgabe_mit_fassung_1_an(tmp_path):
    store, fake, factory = _setup(tmp_path)
    ergebnis = _uebernehmen(store, fake, factory)
    assert ergebnis == {"aufgaben": 3, "kapitel": 2, "uebersprungen": 0}
    assert fake.calls == [(JOB, 1, "verifying")]
    kurs = store.kurs(COURSE)
    assert kurs["title"] == "Beispielkurs" and kurs["outcomes"] == ["Ziel 1"]
    assert kurs["owner_ref"] == "anna" and kurs["default_kind"] == "quiz" and kurs["audience"] == ""
    assert kurs["source_provenance"] == [{"source_id": SRC, "revision": 1, "content_hash": HASH}]
    assert [a["chapter_title"] for a in kurs["aufgaben"]] == ["Kapitel Eins"] * 2 + ["Kapitel Zwei"]
    assert all(len(a["summary"]) == 120 for a in kurs["aufgaben"])
    for a in kurs["aufgaben"]:
        fassungen = store.fassungen(a["task_id"])
        assert [(f["number"], f["based_on"], f["art"], f["author_ref"]) for f in fassungen] == [
            (1, None, "uebernommen", "anna")
        ]
    erste = store.neueste(kurs["aufgaben"][0]["task_id"])["content"]
    assert erste["kind"] == "quiz" and erste["correct_option_ids"] == ["a"]
    assert [z["citation_id"] for z in erste["zitate"]] == [f"{1:036d}"]
    zweite = store.neueste(kurs["aufgaben"][1]["task_id"])["content"]
    assert zweite["kind"] == "open" and zweite["zitate"] == [] and zweite["expected_answer"] == "Antwort"


def test_lektion_haengt_an_erster_aufgabe_des_kapitels(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory)
    a1, a2, a3 = (store.neueste(a["task_id"])["content"] for a in store.kurs(COURSE)["aufgaben"])
    assert a1["lektion"]["title"] == "Lektion A / Lektion B"
    assert "## Lektion A\n\nText A" in a1["lektion"]["body"] and "Text B" in a1["lektion"]["body"]
    assert [c["claim_id"] for c in a1["lektion"]["claims"]] == ["c1"]
    assert a1["lektion"]["concept_ids"] == ["k1"]
    assert a2["lektion"] is None
    assert a3["lektion"]["title"] == "Lektion C" and a3["lektion"]["body"] == "Text C"


def test_uebernahme_ist_einmalig(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory)
    with pytest.raises(ValueError, match="schon_uebernommen"):
        _uebernehmen(store, fake, factory)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(WerkstattAufgabe)) == 3
        assert session.scalar(select(func.count()).select_from(WerkstattFassung)) == 3


def test_unveroeffentlichter_kurs_wird_abgelehnt(tmp_path):
    store, fake, factory = _setup(tmp_path, publiziert=False)
    with pytest.raises(LookupError, match="nicht_veroeffentlicht"):
        _uebernehmen(store, fake, factory)
    with pytest.raises(LookupError, match="kurs_unbekannt"):
        store.kurs(COURSE)


def test_codeaufgaben_werden_uebersprungen_und_gezaehlt(tmp_path):
    store, fake, factory = _setup(tmp_path, _draft(code=True))
    assert _uebernehmen(store, fake, factory) == {"aufgaben": 3, "kapitel": 2, "uebersprungen": 1}
    letzte = store.neueste(store.kurs(COURSE)["aufgaben"][2]["task_id"])["content"]
    # the lesson of chapter 2 moves to its first TAKEN task, since the code task is skipped
    assert letzte["kind"] == "quiz" and letzte["lektion"]["title"] == "Lektion C"


def test_fehler_mitten_drin_hinterlaesst_nichts(tmp_path, monkeypatch):
    store, fake, factory = _setup(tmp_path)
    original = store._schreiben
    zaehler = {"n": 0}

    def kaputt(*args, **kwargs):
        zaehler["n"] += 1
        if zaehler["n"] == 3:
            raise RuntimeError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_schreiben", kaputt)
    with pytest.raises(RuntimeError):
        _uebernehmen(store, fake, factory)
    with factory() as session:
        for modell in (WerkstattAufgabe, WerkstattFassung):
            assert session.scalar(select(func.count()).select_from(modell)) == 0
    with pytest.raises(LookupError):
        store.kurs(COURSE)


def test_entwurf_ist_gueltiger_coursedraft():
    CourseDraft.model_validate(_draft())
