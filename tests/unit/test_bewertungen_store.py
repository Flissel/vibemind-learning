"""Kursbewertungen: eine Zeile je (Kurs, Person), Upsert, bereinigte Stichworte, Meldeliste."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from spaces.learning.services.bewertungen.models import KursBewertung
from spaces.learning.services.bewertungen.store import BewertungFehler, BewertungStore
from spaces.learning.services.db.models import Base


def _engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bewertungen.db'}")
    Base.metadata.create_all(engine)
    return engine


def _zeilen(engine) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(KursBewertung))


def test_abgeben_legt_an_und_ersetzt(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    store = BewertungStore(engine)
    erste = store.abgeben("c1", "p1", "Kurs Eins", "gut", ["Tempo"])
    assert erste["neu"] is True and erste["id"]
    zweite = store.abgeben("c1", "p1", "Kurs Eins", "mittel", [])
    assert zweite["neu"] is False and zweite["id"] == erste["id"]
    eigene = store.eigene("c1", "p1")
    assert eigene["smiley"] == "mittel" and eigene["stichworte"] == [] and eigene["aktualisiert"]
    assert store.eigene("c1", "andere") is None
    assert _zeilen(engine) == 1


def test_doppeltes_abgeben_ohne_integritaetsfehler(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    a, b = BewertungStore(engine), BewertungStore(engine)
    a.abgeben("c1", "p1", "Kurs", "gut", ["x"])
    b.abgeben("c1", "p1", "Kurs", "gut", ["x"])
    assert _zeilen(engine) == 1


def test_integritaetsfehler_wird_als_update_wiederholt(tmp_path: Path, monkeypatch) -> None:
    """Zwei Absendungen laufen gleichzeitig: beide sehen 'keine Zeile', die zweite scheitert am Unique."""
    engine = _engine(tmp_path)
    store = BewertungStore(engine)
    store.abgeben("c1", "p1", "Kurs", "gut", [])
    echte = store._zeile_holen
    aufrufe = {"n": 0}

    def erst_leer(session, course_id, actor_id):
        aufrufe["n"] += 1
        return None if aufrufe["n"] == 1 else echte(session, course_id, actor_id)

    monkeypatch.setattr(store, "_zeile_holen", erst_leer)
    ergebnis = store.abgeben("c1", "p1", "Kurs", "schlecht", [])
    assert ergebnis["neu"] is False
    assert store.eigene("c1", "p1")["smiley"] == "schlecht"
    assert _zeilen(engine) == 1


def test_stichworte_werden_bereinigt(tmp_path: Path) -> None:
    store = BewertungStore(_engine(tmp_path))
    store.abgeben("c1", "p1", "Kurs", "gut", [" a ", "", "b"])
    assert store.eigene("c1", "p1")["stichworte"] == ["a", "b"]
    with pytest.raises(BewertungFehler) as e:
        store.abgeben("c1", "p1", "Kurs", "gut", ["a", "b", "c", "d"])
    assert e.value.code == "zu_viele_stichworte"
    with pytest.raises(BewertungFehler) as e:
        store.abgeben("c1", "p1", "Kurs", "gut", ["x" * 41])
    assert e.value.code == "stichwort_zu_lang"
    with pytest.raises(BewertungFehler) as e:
        store.abgeben("c1", "p1", "Kurs", "super", [])
    assert e.value.code == "smiley_unbekannt"
    assert store.eigene("c1", "p1")["stichworte"] == ["a", "b"]


def test_neue_und_gemeldet(tmp_path: Path) -> None:
    store = BewertungStore(_engine(tmp_path))
    a = store.abgeben("c1", "p1", "Kurs", "gut", ["Tempo"])
    store.abgeben("c1", "p2", "Kurs", "schlecht", [])
    neue = store.neue()
    assert len(neue) == 2 and neue[0]["id"] == a["id"]
    assert all("actor_id" not in n for n in neue)
    assert neue[0]["verteilung"] == {"gut": 1, "mittel": 0, "schlecht": 1}
    assert neue[0]["geaendert"] is False
    assert store.gemeldet_markieren([n["id"] for n in neue]) == 2
    assert store.neue() == []
    store.abgeben("c1", "p1", "Kurs", "mittel", [])
    nochmal = store.neue()
    assert len(nochmal) == 1 and nochmal[0]["geaendert"] is True


def test_uebersicht_zaehlt_stichworte(tmp_path: Path) -> None:
    store = BewertungStore(_engine(tmp_path))
    store.abgeben("c1", "p1", "Kurs", "gut", ["Beispiele"])
    store.abgeben("c1", "p2", "Kurs", "mittel", ["beispiele", "Tempo"])
    store.abgeben("c1", "p3", "Kurs", "schlecht", [])
    uebersicht = store.uebersicht()
    assert len(uebersicht) == 1
    k = uebersicht[0]
    assert k["course_id"] == "c1" and k["kurs_titel"] == "Kurs"
    assert k["verteilung"] == {"gut": 1, "mittel": 1, "schlecht": 1}
    assert k["stichworte_top"][0] == ["Beispiele", 2]
    assert len(k["einzeln"]) == 3
    assert all("actor_id" not in e for e in k["einzeln"])
    assert set(k["einzeln"][0]) == {"smiley", "stichworte", "datum"}


def test_migration_0018_legt_tabelle_an() -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    pfad = Path(__file__).resolve().parents[2] / "services/db/migrations/versions/0018_kurs_bewertungen.py"
    spec = importlib.util.spec_from_file_location("migration_0018", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    assert modul.revision == "0018_kurs_bewertungen" and modul.down_revision == "0017_werkstatt_fach"
    engine = create_engine("sqlite://")
    with engine.begin() as verbindung:
        with Operations.context(MigrationContext.configure(verbindung)):
            modul.upgrade()
        insp = inspect(verbindung)
        assert "learning_course_ratings" in insp.get_table_names()
        namen = {u["name"] for u in insp.get_unique_constraints("learning_course_ratings")}
        assert "uq_learning_course_ratings_course_actor" in namen
        with Operations.context(MigrationContext.configure(verbindung)):
            modul.downgrade()
        assert "learning_course_ratings" not in inspect(verbindung).get_table_names()
