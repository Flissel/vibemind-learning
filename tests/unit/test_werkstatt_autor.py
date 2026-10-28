"""Werkstatt: model roles for the rough plan and one task (fake gateway, no real model)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from spaces.learning.services.course_factory.model_gateway import GatewayResult
from spaces.learning.services.werkstatt.autor import (
    AufgabeAusgabe,
    PrueferAusgabe,
    aufgabe_ausformulieren,
    grobentwurf_erzeugen,
)
from spaces.learning.services.werkstatt.inhalt import Grobentwurf, inhalt_lesen
from spaces.learning.tests.unit.test_werkstatt_pruefung import (
    REFS_2,
    SRC,
    codes,
    lektion,
    quiz,
)


class _Fake:
    def __init__(self, *outputs: object) -> None:
        self.queue = list(outputs)
        self.invocations: list = []

    @property
    def calls(self) -> list[str]:
        return [i.role for i in self.invocations]

    async def generate(self, invocation, output_model):
        self.invocations.append(invocation)
        output = self.queue.pop(0)
        assert isinstance(output, output_model)
        return GatewayResult(output=output, evidence_ref=f"fake://{invocation.role}")


def _autor(**over) -> AufgabeAusgabe:
    return AufgabeAusgabe(aufgabe=inhalt_lesen(quiz(**over)))


def _schlecht() -> AufgabeAusgabe:
    body = "Aufgabe 1: Pruefen Sie eine Datei.\n\n```powershell\nTest-Path C:/Daten/bericht.txt\n```\n"
    return _autor(lektion=lektion(body=body))


def _sauber_pruefer() -> PrueferAusgabe:
    return PrueferAusgabe(issues=[])


async def _ausformulieren(fake: _Fake, **over):
    args = dict(
        correlation_id=str(uuid4()),
        kurs={"course_id": "c1", "title": "Dateien", "audience": "Einsteiger"},
        summary="Dateien pruefen",
        kind="quiz",
        wunsch=None,
        vorherige=None,
        source_refs=REFS_2,
        sources=SRC,
    )
    args.update(over)
    return await aufgabe_ausformulieren(fake, **args)


@pytest.mark.asyncio
async def test_grobentwurf_liefert_kapitel_mit_aufgaben() -> None:
    entwurf = Grobentwurf.model_validate(
        {
            "title": "Dateien",
            "audience": "Einsteiger",
            "default_kind": "quiz",
            "chapters": [{"title": "Kapitel 1", "tasks": [{"summary": "Datei pruefen"}]}],
        }
    )
    fake = _Fake(entwurf)
    ergebnis = await grobentwurf_erzeugen(
        fake,
        correlation_id=str(uuid4()),
        title="Dateien",
        audience="Einsteiger",
        outcomes=["Dateien pruefen"],
        default_kind="quiz",
        hinweise=["kurz halten"],
        source_refs=REFS_2,
    )
    assert ergebnis.chapters[0].tasks[0].summary == "Datei pruefen"
    assert fake.calls == ["werkstatt_grobentwurf"]
    payload = fake.invocations[0].input_payload
    assert payload["default_kind"] == "quiz" and payload["source_refs"][0]["key"] == "S1"


@pytest.mark.asyncio
async def test_ausformulieren_ohne_befunde_braucht_zwei_aufrufe() -> None:
    fake = _Fake(_autor(), _sauber_pruefer())
    erg = await _ausformulieren(fake)
    assert fake.calls == ["werkstatt_aufgabe", "werkstatt_pruefer"]
    assert erg.befunde == []
    assert erg.inhalt["kind"] == "quiz" and erg.zitate and erg.inhalt["zitate"]


@pytest.mark.asyncio
async def test_ausformulieren_repariert_einmal_bei_befunden() -> None:
    fake = _Fake(_schlecht(), _autor(), _sauber_pruefer())
    erg = await _ausformulieren(fake)
    assert fake.calls == ["werkstatt_aufgabe", "werkstatt_aufgabe", "werkstatt_pruefer"]
    assert erg.befunde == []
    feedback = fake.invocations[1].input_payload["reviewer_feedback"]
    assert feedback and all(isinstance(t, str) for t in feedback)


@pytest.mark.asyncio
async def test_ausformulieren_meldet_restbefunde_statt_endlos() -> None:
    fake = _Fake(_schlecht(), _schlecht(), _sauber_pruefer())
    erg = await _ausformulieren(fake)
    assert len(fake.calls) == 3
    assert "lesson_contains_tasks" in codes(erg.befunde)


@pytest.mark.asyncio
async def test_pruefer_befunde_werden_angehaengt() -> None:
    pruefer = PrueferAusgabe(issues=[{"code": "widerspricht_lektion", "text": "Die Antwort widerspricht der Lektion."}])
    fake = _Fake(_autor(), pruefer)
    erg = await _ausformulieren(fake)
    assert codes(erg.befunde) == ["pruefer_widerspricht_lektion"]


@pytest.mark.asyncio
async def test_pruefer_bekommt_nur_zitierte_quellen() -> None:
    fake = _Fake(_autor(), _sauber_pruefer())
    await _ausformulieren(fake)
    refs = fake.invocations[1].input_payload["source_refs"]
    assert [r["chunk_id"] for r in refs] == ["00000000-0000-4000-8000-0000000000b1"]


@pytest.mark.asyncio
async def test_wunsch_und_vorherige_fassung_gehen_ins_modell() -> None:
    fake = _Fake(_autor(), _sauber_pruefer())
    vorherige = quiz()
    await _ausformulieren(fake, wunsch="Mehr Optionen", vorherige=vorherige)
    payload = fake.invocations[0].input_payload
    assert payload["wunsch"] == "Mehr Optionen" and payload["vorherige"] == vorherige
    assert payload["kind"] == "quiz"
