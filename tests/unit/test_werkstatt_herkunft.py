"""Herkunft nachtragen for already taken-over courses, and the Werkstatt keys."""

from __future__ import annotations

from sqlalchemy import update

from spaces.learning.services.werkstatt.freigabe import werkstatt_schluessel
from spaces.learning.services.werkstatt.models import WerkstattAufgabe
from spaces.learning.services.werkstatt.uebernahme import herkunft_nachtragen

from test_werkstatt_uebernahme import COURSE, _draft, _setup, _uebernehmen


def _leeren(factory) -> None:
    with factory.begin() as session:
        session.execute(
            update(WerkstattAufgabe).values(herkunft_key=None, lektion_herkunft_key=None)
        )


def test_herkunft_nachtragen_ordnet_ueber_prompt_zu(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory)
    _leeren(factory)
    assert all(a["herkunft_key"] is None for a in store.kurs(COURSE)["aufgaben"])
    ergebnis = herkunft_nachtragen(store, fake, factory, COURSE)
    assert ergebnis == {"zugeordnet": 3, "schon_zugeordnet": 0, "ohne_zuordnung": []}
    aufgaben = store.kurs(COURSE)["aufgaben"]
    assert [a["herkunft_key"] for a in aufgaben] == ["a1", "a2", "a3"]
    assert [a["lektion_herkunft_key"] for a in aufgaben] == ["l1", None, "l3"]
    # idempotent: a second run changes nothing
    assert herkunft_nachtragen(store, fake, factory, COURSE) == {
        "zugeordnet": 0, "schon_zugeordnet": 3, "ohne_zuordnung": [],
    }


def test_herkunft_nachtragen_ueberschreibt_nichts(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory)
    with factory.begin() as session:
        session.execute(update(WerkstattAufgabe).values(herkunft_key="schon-da"))
    herkunft_nachtragen(store, fake, factory, COURSE)
    assert {a["herkunft_key"] for a in store.kurs(COURSE)["aufgaben"]} == {"schon-da"}


def test_herkunft_nachtragen_meldet_unzuordenbare(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory)
    _leeren(factory)
    neu = store.aufgabe_anlegen(COURSE, chapter_title="Kapitel Eins", summary="selbst")
    store.fassung_anlegen(
        neu["task_id"], content={"kind": "open", "prompt": "Ganz eigene Frage"}, based_on=None,
        author_ref="kursleitung-a", reason="neu", art="neu", findings=[],
    )
    leer = store.aufgabe_anlegen(COURSE, chapter_title="Kapitel Eins", summary="ohne fassung")
    ergebnis = herkunft_nachtragen(store, fake, factory, COURSE)
    assert ergebnis["zugeordnet"] == 3
    assert sorted(ergebnis["ohne_zuordnung"]) == sorted([neu["task_id"], leer["task_id"]])


def test_schluessel_ohne_herkunft_ist_werkstatt_task_id():
    assert werkstatt_schluessel(
        {"task_id": "t1", "herkunft_key": None, "lektion_herkunft_key": None}
    ) == ("werkstatt:t1", "werkstatt:t1:lektion")
    assert werkstatt_schluessel(
        {"task_id": "t1", "herkunft_key": "a1", "lektion_herkunft_key": "l1"}
    ) == ("a1", "l1")
    assert werkstatt_schluessel({"task_id": "t2"}) == ("werkstatt:t2", "werkstatt:t2:lektion")


def test_doppelter_prompt_wird_nicht_zugeordnet(tmp_path):
    # (a) two activities of the draft share one prompt -> neither task is assigned
    entwurf = _draft()
    entwurf["activities"][1]["prompt"] = entwurf["activities"][0]["prompt"]
    store, fake, factory = _setup(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path, entwurf)
    _uebernehmen(store, fake, factory)
    _leeren(factory)
    ergebnis = herkunft_nachtragen(store, fake, factory, COURSE)
    aufgaben = store.kurs(COURSE)["aufgaben"]
    assert ergebnis["zugeordnet"] == 1 and ergebnis["schon_zugeordnet"] == 0
    assert sorted(ergebnis["ohne_zuordnung"]) == sorted(a["task_id"] for a in aufgaben[:2])
    assert [a["herkunft_key"] for a in aufgaben] == [None, None, "a3"]

    # (b) two Werkstatt tasks share one version-1 prompt -> neither is assigned
    store, fake, factory = _setup(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    _uebernehmen(store, fake, factory)
    _leeren(factory)
    eigene = store.aufgabe_anlegen(COURSE, chapter_title="Kapitel Eins", summary="kopie")
    erste = store.neueste(store.kurs(COURSE)["aufgaben"][0]["task_id"])["content"]
    store.fassung_anlegen(
        eigene["task_id"], content=dict(erste), based_on=None, author_ref="kursleitung-a",
        reason="kopie", art="neu", findings=[],
    )
    ergebnis = herkunft_nachtragen(store, fake, factory, COURSE)
    aufgaben = {a["task_id"]: a for a in store.kurs(COURSE)["aufgaben"]}
    assert ergebnis["zugeordnet"] == 2
    assert len(ergebnis["ohne_zuordnung"]) == 2 and eigene["task_id"] in ergebnis["ohne_zuordnung"]
    schluessel = [a["herkunft_key"] for a in aufgaben.values() if a["herkunft_key"]]
    assert sorted(schluessel) == ["a2", "a3"]

    # (c) a key already carried by another task is never given a second time
    store, fake, factory = _setup(tmp_path / "c" if (tmp_path / "c").mkdir() is None else tmp_path)
    _uebernehmen(store, fake, factory)
    _leeren(factory)
    ids = [a["task_id"] for a in store.kurs(COURSE)["aufgaben"]]
    with factory.begin() as session:
        session.get(WerkstattAufgabe, ids[2]).herkunft_key = "a1"
    ergebnis = herkunft_nachtragen(store, fake, factory, COURSE)
    assert ids[0] in ergebnis["ohne_zuordnung"] and ergebnis["schon_zugeordnet"] == 1
    keys = [a["herkunft_key"] for a in store.kurs(COURSE)["aufgaben"] if a["herkunft_key"]]
    assert len(keys) == len(set(keys))
