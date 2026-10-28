"""Werkstatt storage: every change to a task is a new numbered version (Fassung)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.db.models import Base
from spaces.learning.services.werkstatt.store import WerkstattKonflikt, WerkstattStore


def _store(tmp_path: Path) -> WerkstattStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'werkstatt.db'}")
    Base.metadata.create_all(engine)
    return WerkstattStore(sessionmaker(bind=engine, expire_on_commit=False))


def _kurs(s: WerkstattStore, course_id: str = "c1") -> dict:
    return s.kurs_anlegen(
        course_id, title="Terminal", audience="Einsteiger", default_kind="exercise",
        outcomes=["Befehle eingeben"], source_provenance=[{"url": "https://example.org"}],
        owner_ref="kursleitung-a",
    )


def _inhalt(text: str = "Tippe ls") -> dict:
    return {"kind": "exercise", "prompt": text}


def _fassung(s: WerkstattStore, tid: str, based_on, text: str = "x", art: str = "neu") -> dict:
    return s.fassung_anlegen(
        tid, content=_inhalt(text), based_on=based_on, author_ref="kursleitung-a",
        reason="r", art=art, findings=[],
    )


def _aufgabe(s: WerkstattStore, kapitel: str = "Installieren", summary: str = "s") -> dict:
    return s.aufgabe_anlegen("c1", chapter_title=kapitel, summary=summary)


def test_erste_fassung_hat_nummer_1_und_keine_basis(tmp_path):
    s = _store(tmp_path); _kurs(s)
    t = _aufgabe(s, summary="Befehl zur Eingabezeile")
    f = _fassung(s, t["task_id"], None)
    assert f["number"] == 1 and f["based_on"] is None


def test_basis_none_bei_vorhandener_fassung_ist_konflikt(tmp_path):
    s = _store(tmp_path); _kurs(s)
    t = _aufgabe(s)
    _fassung(s, t["task_id"], None)
    with pytest.raises(WerkstattKonflikt) as err:
        _fassung(s, t["task_id"], None)
    assert err.value.neueste["number"] == 1


def test_zweite_fassung_auf_alter_basis_ist_konflikt(tmp_path):
    s = _store(tmp_path); _kurs(s)
    t = _aufgabe(s)
    _fassung(s, t["task_id"], None)
    f2 = _fassung(s, t["task_id"], 1, "zwei")
    assert f2["number"] == 2 and f2["based_on"] == 1
    with pytest.raises(WerkstattKonflikt) as err:
        _fassung(s, t["task_id"], 1, "drei")
    assert err.value.neueste["number"] == 2
    assert len(s.fassungen(t["task_id"])) == 2


def test_task_id_bleibt_ueber_fassungen_gleich(tmp_path):
    s = _store(tmp_path); _kurs(s)
    t = _aufgabe(s)
    _fassung(s, t["task_id"], None)
    _fassung(s, t["task_id"], 1)
    fs = s.fassungen(t["task_id"])
    assert [f["number"] for f in fs] == [1, 2]
    assert {f["task_id"] for f in fs} == {t["task_id"]}
    assert s.neueste(t["task_id"])["number"] == 2


def test_neueste_ohne_fassung_ist_none_und_unbekannte_aufgabe_fehlt(tmp_path):
    s = _store(tmp_path); _kurs(s)
    t = _aufgabe(s)
    assert s.neueste(t["task_id"]) is None
    with pytest.raises(LookupError, match="aufgabe_unbekannt"):
        _fassung(s, "gibt-es-nicht", None)


def test_zurueckholen_legt_neue_fassung_mit_altem_inhalt_an(tmp_path):
    s = _store(tmp_path); _kurs(s)
    t = _aufgabe(s)
    f1 = _fassung(s, t["task_id"], None, "eins")
    _fassung(s, t["task_id"], 1, "zwei")
    neu = s.zurueckholen(t["task_id"], 1, author_ref="kursleitung-a")
    assert neu["number"] == 3 and neu["content"] == f1["content"] and neu["art"] == "zurueckgeholt"
    assert neu["based_on"] == 2


def test_geloeschte_aufgabe_ist_schreibgeschuetzt(tmp_path):
    s = _store(tmp_path); _kurs(s)
    t = _aufgabe(s)
    tid = t["task_id"]
    _fassung(s, tid, None)
    s.aufgabe_loeschen(tid, author_ref="kursleitung-a")
    assert tid not in [a["task_id"] for a in s.kurs("c1")["aufgaben"]]
    with pytest.raises(ValueError, match="aufgabe_geloescht"):
        _fassung(s, tid, 1)
    s.zurueckholen(tid, 1, author_ref="kursleitung-a")
    assert tid in [a["task_id"] for a in s.kurs("c1")["aufgaben"]]


def test_kurs_listet_aufgaben_in_reihenfolge_ohne_geloeschte(tmp_path):
    s = _store(tmp_path); _kurs(s)
    a = _aufgabe(s, "K1", "a"); b = _aufgabe(s, "K1", "b"); c = _aufgabe(s, "K1", "c")
    _fassung(s, b["task_id"], None); _fassung(s, b["task_id"], 1)
    s.aufgabe_loeschen(a["task_id"], author_ref="kursleitung-a")
    aufgaben = s.kurs("c1")["aufgaben"]
    assert [x["task_id"] for x in aufgaben] == [b["task_id"], c["task_id"]]
    assert aufgaben[0]["neueste_fassung"] == 2 and aufgaben[1]["neueste_fassung"] is None


def test_aufgabe_mit_position_schiebt_nachfolger(tmp_path):
    s = _store(tmp_path); _kurs(s)
    a = _aufgabe(s, "K1", "a"); b = _aufgabe(s, "K1", "b")
    c = s.aufgabe_anlegen("c1", chapter_title="K1", summary="c", position=0)
    assert [x["task_id"] for x in s.kurs("c1")["aufgaben"]] == [c["task_id"], a["task_id"], b["task_id"]]


def test_reihenfolge_muss_alle_aufgaben_enthalten(tmp_path):
    s = _store(tmp_path); _kurs(s)
    a = _aufgabe(s, "K1", "a"); b = _aufgabe(s, "K1", "b")
    with pytest.raises(ValueError, match="reihenfolge_unvollstaendig"):
        s.reihenfolge_setzen("c1", [a["task_id"]])
    s.reihenfolge_setzen("c1", [b["task_id"], a["task_id"]])
    assert [x["task_id"] for x in s.kurs("c1")["aufgaben"]] == [b["task_id"], a["task_id"]]


def test_kurs_aendern_und_unbekannter_kurs(tmp_path):
    s = _store(tmp_path); _kurs(s)
    k = s.kurs_aendern("c1", title="Neu")
    assert k["title"] == "Neu" and k["default_kind"] == "exercise"
    with pytest.raises(LookupError):
        s.kurs("nope")


def test_kurs_quellen_setzen_ersetzt_die_provenienz(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _kurs(s)
    neu = [{"source_id": "s1", "revision": 2, "content_hash": "b" * 64}]
    s.kurs_quellen_setzen("c1", neu)
    assert s.kurs("c1")["source_provenance"] == neu
    with pytest.raises(LookupError):
        s.kurs_quellen_setzen("gibt-es-nicht", neu)


def test_kurstitel_mit_500_zeichen_passt_ins_modell(tmp_path):
    # A rough draft and a published CourseDraft both allow 500 characters of title.
    from spaces.learning.services.werkstatt.inhalt import Grobentwurf
    from spaces.learning.services.course_factory.schemas import CourseDraft
    from spaces.learning.services.werkstatt.models import WerkstattKurs

    erlaubt = max(
        Grobentwurf.model_fields["title"].metadata[-1].max_length,
        CourseDraft.model_fields["title"].metadata[-1].max_length,
    )
    assert WerkstattKurs.__table__.c.title.type.length >= erlaubt
    s = _store(tmp_path); _kurs(s)
    lang = "T" * 500
    s.grobentwurf_anwenden("c1", title=lang, default_kind="quiz", kapitel=[])
    assert s.kurs("c1")["title"] == lang


def test_migration_0014_kurstitel_fasst_500_zeichen():
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    pfad = Path(__file__).resolve().parents[2] / "services/db/migrations/versions/0014_werkstatt.py"
    spec = importlib.util.spec_from_file_location("migration_0014_werkstatt", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    engine = create_engine("sqlite://")
    with engine.begin() as verbindung:
        with Operations.context(MigrationContext.configure(verbindung)):
            modul.upgrade()
        spalten = {c["name"]: c["type"] for c in inspect(verbindung).get_columns("learning_werkstatt_kurse")}
    assert spalten["title"].length >= 500


def test_kurs_ohne_fach_ist_terminal(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _kurs(s)
    assert s.kurs("c1")["fach"] == "terminal"


def test_kurs_mit_fach_mathe(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.kurs_anlegen(
        "m1", title="Algebra", audience="Schueler", default_kind="quiz",
        outcomes=["Gleichungen loesen"], source_provenance=[], owner_ref="kursleitung-a",
        fach="mathe",
    )
    assert s.kurs("m1")["fach"] == "mathe"
