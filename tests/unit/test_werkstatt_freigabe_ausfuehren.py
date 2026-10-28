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
        self, course_id: str, *, expected_revision: int, freigabe_id: str, kapitel: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.aufrufe.append({
            "course_id": course_id, "expected_revision": expected_revision,
            "freigabe_id": freigabe_id, "kapitel": kapitel,
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
    fertig = s.freigabe(v["freigabe_id"])
    assert fertig["zustand"] == "freigegeben" and fertig["learnhouse_revision"] == 5
    assert fertig["approval_ref"] == "klick-1" and fertig["freigegeben_at"]


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
    with pytest.raises(ValueError, match="learnhouse_stand_veraltet"):
        await freigabe_ausfuehren(s, gw, v["freigabe_id"], approval_ref="k", learnhouse_revision=4)
    assert gw.aufrufe == []
    assert s.freigabe(v["freigabe_id"])["zustand"] == "angefordert"


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
