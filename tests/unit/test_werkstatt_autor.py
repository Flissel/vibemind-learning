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
    return AufgabeAusgabe(schema_version="werkstatt-aufgabe-v1", aufgabe=inhalt_lesen(quiz(**over)))


def _schlecht() -> AufgabeAusgabe:
    body = "Aufgabe 1: Pruefen Sie eine Datei.\n\n```powershell\nTest-Path C:/Daten/bericht.txt\n```\n"
    return _autor(lektion=lektion(body=body))


def _sauber_pruefer() -> PrueferAusgabe:
    return PrueferAusgabe(schema_version="werkstatt-pruefer-v1", issues=[])


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
            "schema_version": "werkstatt-grobentwurf-v1",
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
    pruefer = PrueferAusgabe(schema_version="werkstatt-pruefer-v1", issues=[{"code": "widerspricht_lektion", "text": "Die Antwort widerspricht der Lektion."}])
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


@pytest.mark.asyncio
async def test_abweichender_typ_ist_befund() -> None:
    fake = _Fake(_autor(), _sauber_pruefer())
    erg = await _ausformulieren(fake, kind="open")
    assert "kind_abweichend" in codes(erg.befunde)


def _durch_echte_validierung(role: str, model: type, content: dict):
    import json

    from spaces.learning.services.course_factory.model_gateway import (
        ROLE_CONTRACTS,
        ModelInvocation,
        _validated_result,
    )

    stage, prompt, schema = ROLE_CONTRACTS[role]
    invocation = ModelInvocation(
        role=role,
        stage=stage,
        correlation_id=str(uuid4()),
        prompt_version=prompt,
        output_schema_version=schema,
        input_payload={},
    )
    body = json.dumps({"id": "c1", "choices": [{"message": {"content": json.dumps(content)}}]}).encode()
    return _validated_result(body, invocation, model)


def test_echte_validierung_nimmt_aufgabe_an() -> None:
    inhalt = {"schema_version": "werkstatt-aufgabe-v1", "aufgabe": quiz()}
    assert _durch_echte_validierung("werkstatt_aufgabe", AufgabeAusgabe, inhalt).output.aufgabe.kind == "quiz"


def test_echte_validierung_nimmt_pruefer_an() -> None:
    inhalt = {"schema_version": "werkstatt-pruefer-v1", "issues": [{"code": "x", "text": "y"}]}
    assert _durch_echte_validierung("werkstatt_pruefer", PrueferAusgabe, inhalt).output.issues[0].code == "x"


def test_echte_validierung_nimmt_grobentwurf_an() -> None:
    inhalt = {
        "schema_version": "werkstatt-grobentwurf-v1",
        "title": "T",
        "audience": "A",
        "default_kind": "quiz",
        "chapters": [{"title": "K", "tasks": [{"summary": "s"}]}],
    }
    assert _durch_echte_validierung("werkstatt_grobentwurf", Grobentwurf, inhalt).output.title == "T"


def test_echte_validierung_lehnt_falsche_schema_version_ab() -> None:
    from spaces.learning.services.course_factory.model_gateway import GatewaySchemaError

    with pytest.raises(GatewaySchemaError):
        _durch_echte_validierung("werkstatt_pruefer", PrueferAusgabe, {"schema_version": "werkstatt-pruefer-v1x", "issues": []})


_CHUNK_1 = "00000000-0000-4000-8000-0000000000b1"
_CHUNK_2 = "00000000-0000-4000-8000-0000000000b2"


def _katalogzitat(chunk: str, citation_id: str, *, revision: int = 1) -> dict:
    return {
        "citation_id": citation_id, "source_id": "00000000-0000-4000-8000-0000000000a1",
        "source_revision": revision, "chunk_id": chunk, "content_hash": "a" * 64,
        "locator": {"page": 1},
    }


@pytest.mark.asyncio
async def test_vorherige_fassung_geht_mit_quellenschluesseln_ins_modell() -> None:
    gueltig = _katalogzitat(_CHUNK_1, "00000000-0000-4000-8000-0000000000c1")
    veraltet = _katalogzitat(_CHUNK_2, "00000000-0000-4000-8000-0000000000c2", revision=7)
    ids = [gueltig["citation_id"], veraltet["citation_id"]]
    vorherige = quiz(
        citation_ids=ids,
        zitate=[gueltig, veraltet],
        lektion=lektion(claims=[{"claim_id": "c1", "text": "Aussage", "citation_ids": ids}]),
    )
    fake = _Fake(_autor(), _sauber_pruefer())
    await _ausformulieren(fake, vorherige=vorherige)
    gesendet = fake.invocations[0].input_payload["vorherige"]
    assert "zitate" not in gesendet
    assert gesendet["citation_ids"] == ["S1"]
    assert gesendet["lektion"]["claims"][0]["citation_ids"] == ["S1"]
    assert vorherige["citation_ids"] == ids  # the stored version itself is untouched


@pytest.mark.asyncio
async def test_zitate_aus_der_modellausgabe_zaehlen_nicht() -> None:
    cid = "00000000-0000-4000-8000-0000000000c1"
    vom_modell = dict(
        citation_ids=[cid],
        zitate=[_katalogzitat(_CHUNK_1, cid)],
        lektion=lektion(claims=[{"claim_id": "c1", "text": "Aussage", "citation_ids": [cid]}]),
    )
    fake = _Fake(_autor(**vom_modell), _autor(**vom_modell), _sauber_pruefer())
    erg = await _ausformulieren(fake)
    assert erg.inhalt["citation_ids"] == [] and erg.zitate == []
    assert erg.inhalt["lektion"]["claims"][0]["citation_ids"] == []
    assert "claim_uncited" in codes(erg.befunde)
