"""Herkunft nachtragen for already taken-over courses, the Werkstatt keys, and the first
release of a taken-over course."""

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


def test_herkunft_nachtragen_fuellt_fehlenden_lektionsschluessel(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory)
    with factory.begin() as session:
        session.execute(update(WerkstattAufgabe).values(lektion_herkunft_key=None))
    ergebnis = herkunft_nachtragen(store, fake, factory, COURSE)
    assert ergebnis == {"zugeordnet": 2, "schon_zugeordnet": 3, "ohne_zuordnung": []}
    aufgaben = store.kurs(COURSE)["aufgaben"]
    assert [a["herkunft_key"] for a in aufgaben] == ["a1", "a2", "a3"]
    assert [a["lektion_herkunft_key"] for a in aufgaben] == ["l1", None, "l3"]


# The first release of a taken-over course: compared with the taken-over Fassungen, origin
# keys required, the lessons merged at takeover leave LearnHouse once.


def _entwurf(fake):
    from spaces.learning.services.course_factory.schemas import CourseDraft

    return lambda: CourseDraft.model_validate(fake.draft)


def _vorbereiten(store, entwurf=None, revision=4):
    from spaces.learning.services.werkstatt.freigabe import freigabe_vorbereiten

    extra = {} if entwurf is None else {"entwurf": entwurf}
    return freigabe_vorbereiten(
        store, COURSE, autor="kursleitung-a", learnhouse_revision=revision, **extra
    )


def _neue_fassung(store, task_id, aenderung):
    neueste = store.neueste(task_id)
    store.fassung_anlegen(
        task_id, content={**neueste["content"], **aenderung}, based_on=neueste["number"],
        author_ref="kursleitung-a", reason="r", art="neu", findings=[],
    )


def test_erste_freigabe_nach_uebernahme_vergleicht_mit_uebernommenem_stand(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)
    t1, t2, t3 = [a["task_id"] for a in store.kurs(COURSE)["aufgaben"]]
    _neue_fassung(store, t2, {"expected_answer": "Eine andere Antwort"})
    r = _vorbereiten(store)
    assert r["vergleich"]["neu"] == []
    assert [(e["task_id"], e["fassung_alt"], e["fassung_neu"]) for e in r["vergleich"]["geaendert"]] == [
        (t2, 1, 2)
    ]
    assert r["vergleich"]["unveraendert"] == 2
    assert r["vergleich"]["geloescht"] == [] and r["vergleich"]["textkorrektur"] == []


def test_unveraendert_uebernommener_kurs_hat_nichts_zu_veroeffentlichen(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)
    r = _vorbereiten(store, _entwurf(fake))
    assert r["vergleich"]["unveraendert"] == 3 and r["nichts_zu_veroeffentlichen"] is True
    assert r["vergleich"]["offline"] == []


def test_geloeschte_uebernommene_aufgabe_nimmt_ihre_fabrik_schluessel_offline(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)
    t1 = store.kurs(COURSE)["aufgaben"][0]["task_id"]
    store.aufgabe_loeschen(t1, author_ref="kursleitung-a")
    r = _vorbereiten(store, _entwurf(fake))
    assert [e["task_id"] for e in r["vergleich"]["geloescht"]] == [t1]
    assert r["vergleich"]["offline"] == [
        {"key": "l1", "art": "lesson", "titel": "Lektion A / Lektion B"},
        {"key": "a1", "art": "task", "titel": "Frage a1: " + "x" * 110},
    ]


def test_erste_freigabe_entfernt_zusammengefuehrte_lektionen(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)
    r = _vorbereiten(store, _entwurf(fake))
    # l1 and l2 were joined into the section of a1; l2 stays published until this release.
    assert r["vergleich"]["zusammengefuehrt"] == [
        {"key": "l2", "art": "lesson", "titel": "Lektion B"},
    ]
    assert not [b for b in r["befunde"] if b["code"] == "zusammengefuehrte_lektionen_unbekannt"]


def test_unlesbarer_entwurf_meldet_zusammengefuehrte_lektionen_unbekannt(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)

    def kaputt():
        raise ValueError("entwurf_nicht_lesbar")

    r = _vorbereiten(store, kaputt)
    (befund,) = [b for b in r["befunde"] if b["code"] == "zusammengefuehrte_lektionen_unbekannt"]
    assert befund["blockierend"] is False and befund["task_id"] is None and befund["text"]
    assert r["vergleich"]["zusammengefuehrt"] == []
    assert r["blockiert"] is False


def test_uebernommene_aufgabe_ohne_herkunft_sperrt(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)
    t1, t2, _t3 = [a["task_id"] for a in store.kurs(COURSE)["aufgaben"]]
    with factory.begin() as session:
        session.get(WerkstattAufgabe, t2).herkunft_key = None
    r = _vorbereiten(store, _entwurf(fake))
    (befund,) = [b for b in r["befunde"] if b["code"] == "herkunft_fehlt"]
    assert befund["task_id"] == t2 and befund["blockierend"] is True
    assert "Frage a2" in befund["text"] and r["blockiert"] is True


def test_lektionstraegerin_ohne_lektionsherkunft_sperrt(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)
    t1 = store.kurs(COURSE)["aufgaben"][0]["task_id"]
    with factory.begin() as session:
        session.get(WerkstattAufgabe, t1).lektion_herkunft_key = None
    r = _vorbereiten(store, _entwurf(fake))
    assert [b["task_id"] for b in r["befunde"] if b["code"] == "herkunft_fehlt"] == [t1]
    assert r["blockiert"] is True


def test_selbst_angelegte_aufgabe_braucht_keine_herkunft(tmp_path):
    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)
    eigene = store.aufgabe_anlegen(COURSE, chapter_title="Kapitel Eins", summary="eigene")
    store.fassung_anlegen(
        eigene["task_id"], content=store.neueste(store.kurs(COURSE)["aufgaben"][1]["task_id"])["content"]
        | {"prompt": "Ganz eigene Frage?"},
        based_on=None, author_ref="kursleitung-a", reason="neu", art="neu", findings=[],
    )
    r = _vorbereiten(store, _entwurf(fake))
    assert [b for b in r["befunde"] if b["code"] == "herkunft_fehlt"] == []
    assert [e["task_id"] for e in r["vergleich"]["neu"]] == [eigene["task_id"]]


def test_zusammengefuehrte_lektionen_werden_nur_bei_der_ersten_freigabe_entfernt(tmp_path):
    import asyncio

    from spaces.learning.services.werkstatt.freigabe import freigabe_ausfuehren
    from spaces.learning.tests.unit.test_werkstatt_freigabe_ausfuehren import FakeGateway

    store, fake, factory = _setup(tmp_path)
    _uebernehmen(store, fake, factory, katalog=None)
    t1, t2, _t3 = [a["task_id"] for a in store.kurs(COURSE)["aufgaben"]]
    _neue_fassung(store, t2, {"expected_answer": "Eine andere Antwort"})
    gw = FakeGateway()
    erste = _vorbereiten(store, _entwurf(fake))
    asyncio.run(freigabe_ausfuehren(
        store, gw, erste["freigabe_id"], approval_ref="k1", learnhouse_revision=4
    ))
    assert gw.aufrufe[0]["entfernen"] == ["l2"]
    _neue_fassung(store, t1, {"prompt": "Eine neue Frage?"})
    zweite = _vorbereiten(store, _entwurf(fake), revision=5)
    assert zweite["vergleich"]["zusammengefuehrt"] == []
    asyncio.run(freigabe_ausfuehren(
        store, gw, zweite["freigabe_id"], approval_ref="k2", learnhouse_revision=5
    ))
    assert gw.aufrufe[1]["entfernen"] == []
