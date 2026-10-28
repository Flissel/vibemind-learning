"""Werkstatt jobs: store claim logic and the worker that runs model jobs (fake gateway)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.course_factory.model_gateway import (
    GatewayResult,
    GatewaySchemaError,
    GatewayUnavailable,
)
from spaces.learning.services.db.models import Base
from spaces.learning.services.werkstatt.autor import AufgabeAusgabe, PrueferAusgabe
from spaces.learning.services.werkstatt.auftraege import WerkstattArbeiter
from spaces.learning.services.werkstatt.inhalt import Grobentwurf, inhalt_lesen
from spaces.learning.services.werkstatt.store import WerkstattStore
from spaces.learning.tests.unit.test_werkstatt_pruefung import REFS_2, SRC, quiz

_SRC_ID = "00000000-0000-4000-8000-0000000000a1"
_PROV = [{"source_id": _SRC_ID, "revision": 1, "content_hash": "a" * 64}]


def _store(tmp_path: Path) -> WerkstattStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'werkstatt.db'}")
    Base.metadata.create_all(engine)
    return WerkstattStore(sessionmaker(bind=engine, expire_on_commit=False))


def _kurs(s: WerkstattStore) -> None:
    s.kurs_anlegen(
        "c1", title="Neu", audience="Einsteiger", default_kind="quiz",
        outcomes=["Dateien pruefen"], source_provenance=_PROV, owner_ref="kursleitung-a",
    )


@dataclass
class _Katalog:
    refs: tuple[str, ...] = REFS_2
    fehler: Exception | None = None
    gesehen: list = None

    def load(self, *, course_id, provenance):
        self.gesehen = [course_id, provenance]
        if self.fehler:
            raise self.fehler
        return type("K", (), {"snapshots": SRC, "model_refs": self.refs})()


class _Fake:
    def __init__(self, *outputs: object) -> None:
        self.queue = list(outputs)
        self.closed = False

    async def generate(self, invocation, output_model):
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, output_model)
        return GatewayResult(output=item, evidence_ref="fake://x")

    async def aclose(self) -> None:
        self.closed = True


def _arbeiter(store, fake, katalog=None) -> WerkstattArbeiter:
    return WerkstattArbeiter(store, lambda: fake, katalog or _Katalog())


def _grob() -> Grobentwurf:
    return Grobentwurf(
        schema_version="werkstatt-grobentwurf-v1", title="Dateien", audience="Einsteiger",
        default_kind="open",
        chapters=[
            {"title": "K1", "tasks": [{"summary": "A"}, {"summary": "B"}]},
            {"title": "K2", "tasks": [{"summary": "C"}]},
        ],
    )


def _aufgabe(**over) -> AufgabeAusgabe:
    return AufgabeAusgabe(schema_version="werkstatt-aufgabe-v1", aufgabe=inhalt_lesen(quiz(**over)))


def _pruefer() -> PrueferAusgabe:
    return PrueferAusgabe(schema_version="werkstatt-pruefer-v1", issues=[])


def _ausformulieren_fake() -> _Fake:
    return _Fake(_aufgabe(), _pruefer())


@pytest.mark.asyncio
async def test_grobentwurf_auftrag_legt_aufgaben_ohne_fassung_an(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _kurs(s)
    a = s.auftrag_anlegen("c1", kind="grobentwurf", input={})["id"]
    fake = _Fake(_grob())
    katalog = _Katalog()
    assert await _arbeiter(s, fake, katalog).einen_ausfuehren() is True
    auftrag = s.auftrag(a)
    assert auftrag["state"] == "fertig"
    ids = auftrag["result"]["task_ids"]
    assert len(ids) == 3
    kurs = s.kurs("c1")
    assert kurs["title"] == "Dateien" and kurs["default_kind"] == "open"
    assert [x["summary"] for x in kurs["aufgaben"]] == ["A", "B", "C"]
    assert all(s.fassungen(t) == [] for t in ids)
    assert fake.closed
    prov = katalog.gesehen[1][0]
    assert (prov.source_id, prov.revision, prov.content_hash) == (_SRC_ID, 1, "a" * 64)
    assert await _arbeiter(s, fake).einen_ausfuehren() is False


@pytest.mark.asyncio
async def test_ausformulieren_auftrag_speichert_fassung_mit_befunden(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _kurs(s)
    t = s.aufgabe_anlegen("c1", chapter_title="K", summary="Dateien pruefen")["task_id"]
    a = s.auftrag_anlegen("c1", kind="ausformulieren", task_id=t, input={"based_on": None})["id"]
    fake = _Fake(_aufgabe(), PrueferAusgabe(
        schema_version="werkstatt-pruefer-v1", issues=[{"code": "zu_lang", "text": "Zu lang."}]))
    await _arbeiter(s, fake).einen_ausfuehren()
    auftrag = s.auftrag(a)
    assert auftrag["state"] == "fertig", auftrag
    fassung = auftrag["result"]["fassung"]
    assert fassung["number"] == 1 and fassung["based_on"] is None and fassung["art"] == "neu"
    assert [f["code"] for f in fassung["findings"]] == ["pruefer_zu_lang"]
    assert s.neueste(t)["content"]["kind"] == "quiz"


@pytest.mark.asyncio
async def test_auftrag_fehler_speichert_keine_fassung(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _kurs(s)
    t = s.aufgabe_anlegen("c1", chapter_title="K", summary="x")["task_id"]
    a = s.auftrag_anlegen("c1", kind="ausformulieren", task_id=t, input={"based_on": None})["id"]
    await _arbeiter(s, _Fake(GatewayUnavailable("weg"))).einen_ausfuehren()
    assert s.auftrag(a)["state"] == "fehler" and s.fassungen(t) == []
    assert s.auftrag(a)["error"] == "modell_nicht_erreichbar"

    b = s.auftrag_anlegen("c1", kind="ausformulieren", task_id=t, input={"based_on": None})["id"]
    await _arbeiter(s, _Fake(GatewaySchemaError("muell"))).einen_ausfuehren()
    assert s.auftrag(b)["error"] == "modell_ausgabe_ungueltig" and s.fassungen(t) == []

    c = s.auftrag_anlegen("c1", kind="ausformulieren", task_id=t, input={"based_on": None})["id"]
    await _arbeiter(s, _Fake(_aufgabe()), _Katalog(fehler=ValueError("stale"))).einen_ausfuehren()
    assert s.auftrag(c)["error"] == "quellen_nicht_bereit" and s.fassungen(t) == []

    d = s.auftrag_anlegen("c1", kind="ausformulieren", task_id=t, input={"based_on": None})["id"]
    await _arbeiter(s, _Fake(RuntimeError("boom"))).einen_ausfuehren()
    assert s.auftrag(d)["error"] == "interner_fehler" and s.fassungen(t) == []


@pytest.mark.asyncio
async def test_auftrag_nutzt_aktuelle_quellrevision(tmp_path: Path) -> None:
    from spaces.learning.services.course_factory.repository import SourceProvenance

    s = _store(tmp_path)
    _kurs(s)
    t = s.aufgabe_anlegen("c1", chapter_title="K", summary="x")["task_id"]
    a = s.auftrag_anlegen("c1", kind="ausformulieren", task_id=t, input={"based_on": None})["id"]
    katalog = _Katalog()
    aufrufe: list = []

    def aktuell(course_id, source_ids):
        aufrufe.append((course_id, source_ids))
        return (SourceProvenance(source_id=_SRC_ID, revision=2, content_hash="b" * 64),)

    arbeiter = WerkstattArbeiter(s, lambda: _Fake(_aufgabe(), _pruefer()), katalog, aktuell)
    await arbeiter.einen_ausfuehren()
    assert s.auftrag(a)["state"] == "fertig", s.auftrag(a)
    assert aufrufe == [("c1", (_SRC_ID,))]
    prov = katalog.gesehen[1][0]
    assert (prov.revision, prov.content_hash) == (2, "b" * 64)
    assert s.kurs("c1")["source_provenance"] == [
        {"source_id": _SRC_ID, "revision": 2, "content_hash": "b" * 64}
    ]


@pytest.mark.asyncio
async def test_konflikt_wird_als_fehler_mit_neuester_gemeldet(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _kurs(s)
    t = s.aufgabe_anlegen("c1", chapter_title="K", summary="x")["task_id"]
    s.fassung_anlegen(
        t, content=quiz(), based_on=None, author_ref="kursleitung-a", reason="von Hand", art="neu",
        findings=[],
    )
    a = s.auftrag_anlegen("c1", kind="ausformulieren", task_id=t, input={"based_on": None})["id"]
    await _arbeiter(s, _ausformulieren_fake()).einen_ausfuehren()
    auftrag = s.auftrag(a)
    assert auftrag["state"] == "fehler" and auftrag["error"] == "konflikt"
    assert auftrag["result"]["neueste"]["number"] == 1
    assert len(s.fassungen(t)) == 1


def test_haengende_auftraege_werden_beim_start_abgebrochen(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _kurs(s)
    a = s.auftrag_anlegen("c1", kind="grobentwurf", input={})["id"]
    b = s.auftrag_anlegen("c1", kind="grobentwurf", input={})["id"]
    assert s.naechster_auftrag()["id"] == a  # now running
    _arbeiter(s, _Fake()).haengende_abbrechen()
    assert s.auftrag(a)["state"] == "fehler" and s.auftrag(a)["error"] == "abgebrochen"
    assert s.auftrag(b)["state"] == "queued"


def test_naechster_auftrag_vergibt_jeden_auftrag_nur_einmal(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _kurs(s)
    ids = [s.auftrag_anlegen("c1", kind="grobentwurf", input={})["id"] for _ in range(2)]
    erste, zweite = s.naechster_auftrag(), s.naechster_auftrag()
    assert {erste["id"], zweite["id"]} == set(ids) and erste["state"] == "running"
    assert s.naechster_auftrag() is None



@pytest.mark.asyncio
async def test_grobentwurf_fehler_laesst_kurs_unveraendert(tmp_path: Path, monkeypatch) -> None:
    from spaces.learning.services.werkstatt import store as store_modul

    s = _store(tmp_path)
    _kurs(s)
    vorher = s.aufgabe_anlegen("c1", chapter_title="Alt", summary="alt")
    echt = store_modul.WerkstattStore._aufgabe_einfuegen
    aufrufe: list[int] = []

    def kaputt(self, session, *args, **kwargs):
        aufrufe.append(1)
        if len(aufrufe) == 2:
            raise RuntimeError("db kaputt")
        return echt(self, session, *args, **kwargs)

    monkeypatch.setattr(store_modul.WerkstattStore, "_aufgabe_einfuegen", kaputt)
    a = s.auftrag_anlegen("c1", kind="grobentwurf", input={})["id"]
    await _arbeiter(s, _Fake(_grob())).einen_ausfuehren()
    assert s.auftrag(a)["state"] == "fehler" and s.auftrag(a)["error"] == "interner_fehler"
    kurs = s.kurs("c1")
    assert kurs["title"] == "Neu" and kurs["default_kind"] == "quiz"
    assert [x["task_id"] for x in kurs["aufgaben"]] == [vorher["task_id"]]
