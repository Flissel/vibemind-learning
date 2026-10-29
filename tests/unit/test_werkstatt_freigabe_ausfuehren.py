"""Freigabe ausfuehren: das freigegebene Manifest wird nach LearnHouse geschrieben."""

from __future__ import annotations

from typing import Any

import pytest

from spaces.learning.services.course_factory.learnhouse_gateway import LearnHouseGatewayError
from spaces.learning.services.werkstatt.freigabe import freigabe_ausfuehren
from spaces.learning.tests.unit.test_werkstatt_freigabe import (
    COURSE,
    _aufgabe,
    _neue_fassung,
    _quiz,
    _store,
    _vorbereiten,
)


class FakeGateway:
    def __init__(self, fehler: bool = False) -> None:
        self.aufrufe: list[dict[str, Any]] = []
        self.fehler = fehler

    async def werkstatt_stand_schreiben(
        self, course_id: str, *, expected_revision: int, freigabe_id: str,
        kapitel: list[dict[str, Any]], entfernen: list[str] | None = None, **extra: Any,
    ) -> dict[str, Any]:
        self.aufrufe.append({
            "course_id": course_id, "expected_revision": expected_revision,
            "freigabe_id": freigabe_id, "kapitel": kapitel, "entfernen": entfernen, **extra,
        })
        if self.fehler:
            raise LearnHouseGatewayError("LearnHouse is unavailable")
        return {"revision": expected_revision + 1, "aktualisiert": 0, "neu": 2, "unveroeffentlicht": 0}


@pytest.mark.asyncio
async def test_freigabe_schreibt_manifest_nach_learnhouse(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz(), kapitel="Kapitel 1")
    t2 = _aufgabe(s, _quiz("Wie wechselt man den Ordner?", "b"), kapitel="Kapitel 1")
    inhalt_ohne_lektion = _quiz("Dritte Frage?")
    del inhalt_ohne_lektion["lektion"]
    t3 = _aufgabe(s, inhalt_ohne_lektion, kapitel="Kapitel 2")
    v = _vorbereiten(s, revision=4)
    # A newer version after preparing must not leak into the write: the manifest is what was approved.
    _neue_fassung(s, t1, _quiz("Ganz andere Frage?"))
    gw = FakeGateway()

    r = await freigabe_ausfuehren(s, gw, v["freigabe_id"], approval_ref="klick-1", learnhouse_revision=4)

    assert r == {"learnhouse_revision": 5, "aktualisiert": 0, "neu": 2, "unveroeffentlicht": 0}
    (aufruf,) = gw.aufrufe
    assert aufruf["course_id"] == COURSE and aufruf["expected_revision"] == 4
    assert aufruf["freigabe_id"] == v["freigabe_id"]
    k1, k2 = aufruf["kapitel"]
    assert k1["titel"] == "Kapitel 1" and k2["titel"] == "Kapitel 2"
    assert [(e["key"], e["art"]) for e in k1["eintraege"]] == [
        (f"werkstatt:{t1}:lektion", "lesson"), (f"werkstatt:{t1}", "task"),
        (f"werkstatt:{t2}:lektion", "lesson"), (f"werkstatt:{t2}", "task"),
    ]
    lektion, aufgabe = k1["eintraege"][0], k1["eintraege"][1]
    assert lektion["titel"] == "Dateien" and lektion["markdown"].startswith("Mit ls")
    assert lektion["claims"] == []
    assert aufgabe["prompt"] == "Welcher Befehl listet Dateien auf?"
    assert aufgabe["titel"] == aufgabe["prompt"][:120]
    assert [(e["key"], e["art"]) for e in k2["eintraege"]] == [(f"werkstatt:{t3}", "task")]
    # A course without fach is a terminal course: no formula typesetting switch (Ruling 6).
    assert "math" not in aufruf
    fertig = s.freigabe(v["freigabe_id"])
    assert fertig["zustand"] == "freigegeben" and fertig["learnhouse_revision"] == 5
    assert fertig["approval_ref"] == "klick-1" and fertig["freigegeben_at"]


def _mathe_store(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from spaces.learning.services.db.models import Base
    from spaces.learning.services.werkstatt.store import WerkstattStore

    engine = create_engine(f"sqlite:///{tmp_path / 'mathe.db'}")
    Base.metadata.create_all(engine)
    s = WerkstattStore(sessionmaker(bind=engine, expire_on_commit=False))
    s.kurs_anlegen(
        COURSE, title="Kurvendiskussion", audience="Oberstufe", default_kind="quiz",
        outcomes=["Extrema bestimmen"], source_provenance=[], owner_ref="kursleitung-a", fach="mathe",
    )
    return s


@pytest.mark.asyncio
async def test_mathe_kurs_schickt_formelsatz_fuer_lektionen(tmp_path):
    s = _mathe_store(tmp_path)
    _aufgabe(s, _quiz("Welchen Wert hat $f(1)$ für $f(x)=x^2$?"))
    gw = FakeGateway()

    await freigabe_ausfuehren(s, gw, _vorbereiten(s)["freigabe_id"], approval_ref="k", learnhouse_revision=1)

    (aufruf,) = gw.aufrufe
    assert aufruf["math"] is True
    aufgabe = aufruf["kapitel"][0]["eintraege"][1]
    # The LearnHouse activity name shows the formula text without its dollars.
    assert aufgabe["titel"] == "Welchen Wert hat f(1) für f(x)=x^2?"
    assert aufgabe["prompt"] == "Welchen Wert hat $f(1)$ für $f(x)=x^2$?"


def test_aktivitaetstitel_schneidet_nie_in_einer_formel():
    from spaces.learning.services.werkstatt.freigabe import aktivitaetstitel

    lang = "Bestimmen Sie " + "x" * 100 + " zu $\\frac{x^2-1}{x-1}$ genau."
    titel = aktivitaetstitel(lang, mathe=True)
    assert titel == "Bestimmen Sie " + "x" * 100 + " zu"
    assert len(aktivitaetstitel("a" * 300, mathe=True)) == 120
    assert aktivitaetstitel("Mit $$x^2$$ und \\$5", mathe=True) == "Mit x^2 und $5"
    # Terminal courses keep the plain cut, dollars included.
    terminal = "Was zeigt $env:Path an? " + "y" * 200
    assert aktivitaetstitel(terminal, mathe=False) == terminal[:120]


@pytest.mark.asyncio
async def test_leere_freigabe_schreibt_nichts(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    gw = FakeGateway()
    await freigabe_ausfuehren(s, gw, _vorbereiten(s)["freigabe_id"], approval_ref="k", learnhouse_revision=1)
    zweite = _vorbereiten(s)
    assert zweite["nichts_zu_veroeffentlichen"] is True
    with pytest.raises(ValueError, match="nichts_zu_veroeffentlichen"):
        await freigabe_ausfuehren(s, gw, zweite["freigabe_id"], approval_ref="k2", learnhouse_revision=1)
    assert len(gw.aufrufe) == 1
    assert s.freigabe(zweite["freigabe_id"])["zustand"] == "angefordert"


@pytest.mark.asyncio
async def test_veraltete_learnhouse_revision_bricht_ab(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    v = _vorbereiten(s, revision=3)
    gw = FakeGateway()
    # Two steps ahead: someone else changed the course (one step ahead may be our own lost write).
    with pytest.raises(ValueError, match="learnhouse_stand_veraltet"):
        await freigabe_ausfuehren(s, gw, v["freigabe_id"], approval_ref="k", learnhouse_revision=5)
    assert gw.aufrufe == []
    assert s.freigabe(v["freigabe_id"])["zustand"] == "angefordert"


class _VerloreneAntwort:
    """LearnHouse as the fork behaves: it commits, then the answer is lost once; a repeat with
    the same freigabe_id returns the stored result, another body is refused."""

    def __init__(self, revision: int) -> None:
        self.revision = revision
        self.erledigt: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.aufrufe: list[dict[str, Any]] = []
        self.verlieren = True

    async def werkstatt_stand_schreiben(self, course_id, *, expected_revision, freigabe_id,
                                        kapitel, entfernen=None):
        self.aufrufe.append({"expected_revision": expected_revision, "freigabe_id": freigabe_id})
        koerper = (kapitel, entfernen)
        if freigabe_id in self.erledigt:
            gespeichert, ergebnis = self.erledigt[freigabe_id]
            if gespeichert != koerper:
                raise LearnHouseGatewayError("rejected with 409", status=409, code="freigabe_id_reused")
            return ergebnis
        if expected_revision != self.revision:
            raise LearnHouseGatewayError("rejected with 409", status=409, code="revision_conflict")
        self.revision += 1
        ergebnis = {"revision": self.revision, "aktualisiert": 1, "neu": 0, "unveroeffentlicht": 0}
        self.erledigt[freigabe_id] = (koerper, ergebnis)
        if self.verlieren:
            self.verlieren = False
            raise LearnHouseGatewayError("LearnHouse is unavailable")
        return ergebnis


@pytest.mark.asyncio
async def test_wiederaufnahme_nach_verlorener_antwort(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    v = _vorbereiten(s, revision=4)
    lh = _VerloreneAntwort(revision=4)
    with pytest.raises(LearnHouseGatewayError):
        await freigabe_ausfuehren(s, lh, v["freigabe_id"], approval_ref="k1", learnhouse_revision=4)
    assert s.freigabe(v["freigabe_id"])["zustand"] == "angefordert" and lh.revision == 5
    # LearnHouse already stands at vorher + 1: the same write again picks up the stored result.
    r = await freigabe_ausfuehren(s, lh, v["freigabe_id"], approval_ref="k2", learnhouse_revision=5)
    assert r["learnhouse_revision"] == 5 and r["aktualisiert"] == 1
    assert [a["expected_revision"] for a in lh.aufrufe] == [4, 4]
    assert {a["freigabe_id"] for a in lh.aufrufe} == {v["freigabe_id"]}
    fertig = s.freigabe(v["freigabe_id"])
    assert fertig["zustand"] == "freigegeben" and fertig["learnhouse_revision"] == 5
    assert lh.revision == 5  # nothing was written twice


@pytest.mark.asyncio
async def test_fremde_aenderung_um_eins_bleibt_stand_veraltet(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    v = _vorbereiten(s, revision=4)
    lh = _VerloreneAntwort(revision=5)  # someone else moved the course one step
    with pytest.raises(ValueError, match="learnhouse_stand_veraltet"):
        await freigabe_ausfuehren(s, lh, v["freigabe_id"], approval_ref="k", learnhouse_revision=5)
    assert s.freigabe(v["freigabe_id"])["zustand"] == "angefordert"


@pytest.mark.asyncio
async def test_freigabe_schickt_offline_schluessel_als_entfernen(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz())
    _aufgabe(s, _quiz("Zweite Frage?"))
    gw = FakeGateway()
    await freigabe_ausfuehren(s, gw, _vorbereiten(s)["freigabe_id"], approval_ref="k", learnhouse_revision=1)
    assert gw.aufrufe[0]["entfernen"] == []
    s.aufgabe_loeschen(t1, author_ref="kursleitung-a")
    zweite = _vorbereiten(s, revision=2)
    await freigabe_ausfuehren(s, gw, zweite["freigabe_id"], approval_ref="k2", learnhouse_revision=2)
    assert gw.aufrufe[1]["entfernen"] == [f"werkstatt:{t1}:lektion", f"werkstatt:{t1}"]


@pytest.mark.asyncio
async def test_gateway_fehler_laesst_freigabe_offen(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    v = _vorbereiten(s)
    with pytest.raises(LearnHouseGatewayError):
        await freigabe_ausfuehren(s, FakeGateway(fehler=True), v["freigabe_id"], approval_ref="k", learnhouse_revision=1)
    offen = s.freigabe(v["freigabe_id"])
    assert offen["zustand"] == "angefordert" and offen["learnhouse_revision"] is None


@pytest.mark.asyncio
async def test_freigegeben_ist_endgueltig(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    v = _vorbereiten(s)
    gw = FakeGateway()
    await freigabe_ausfuehren(s, gw, v["freigabe_id"], approval_ref="k", learnhouse_revision=1)
    with pytest.raises(ValueError, match="freigabe_nicht_offen"):
        await freigabe_ausfuehren(s, gw, v["freigabe_id"], approval_ref="k", learnhouse_revision=2)
    assert len(gw.aufrufe) == 1


@pytest.mark.asyncio
async def test_blockierte_freigabe_wird_nicht_geschrieben(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, None)  # a task without any content blocks the release
    v = _vorbereiten(s)
    assert v["blockiert"] is True
    gw = FakeGateway()
    with pytest.raises(ValueError, match="freigabe_blockiert"):
        await freigabe_ausfuehren(s, gw, v["freigabe_id"], approval_ref="k", learnhouse_revision=1)
    assert gw.aufrufe == []


class _KonfliktGateway:
    def __init__(self, status: int, code: str | None) -> None:
        self.status, self.code = status, code

    async def werkstatt_stand_schreiben(self, course_id, **_kw):
        raise LearnHouseGatewayError(
            f"LearnHouse request was rejected with {self.status}", status=self.status, code=self.code
        )


@pytest.mark.asyncio
async def test_learnhouse_revisionskonflikt_wird_stand_veraltet(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    v = _vorbereiten(s)
    with pytest.raises(ValueError, match="learnhouse_stand_veraltet"):
        await freigabe_ausfuehren(
            s, _KonfliktGateway(409, "revision_conflict"), v["freigabe_id"],
            approval_ref="k", learnhouse_revision=1,
        )
    assert s.freigabe(v["freigabe_id"])["zustand"] == "angefordert"


@pytest.mark.asyncio
async def test_wiederverwendete_freigabe_id_wird_gemeldet(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    v = _vorbereiten(s)
    with pytest.raises(ValueError, match="freigabe_id_reused"):
        await freigabe_ausfuehren(
            s, _KonfliktGateway(409, "freigabe_id_reused"), v["freigabe_id"],
            approval_ref="k", learnhouse_revision=1,
        )
    assert s.freigabe(v["freigabe_id"])["zustand"] == "angefordert"
    with pytest.raises(LearnHouseGatewayError):
        await freigabe_ausfuehren(
            s, _KonfliktGateway(500, None), v["freigabe_id"], approval_ref="k", learnhouse_revision=1
        )


@pytest.mark.asyncio
async def test_fehlende_manifest_fassung_wird_gemeldet(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    v = _vorbereiten(s)
    with s._sessions.begin() as session:
        from spaces.learning.services.werkstatt.models import WerkstattFreigabe
        row = session.get(WerkstattFreigabe, v["freigabe_id"])
        row.manifest = [dict(row.manifest[0], fassung=99)]
    with pytest.raises(ValueError, match="manifest_fassung_fehlt"):
        await freigabe_ausfuehren(s, FakeGateway(), v["freigabe_id"], approval_ref="k", learnhouse_revision=1)
