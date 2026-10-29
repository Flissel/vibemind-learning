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
    assert [r["key"] for r in refs] == ["S1"]


@pytest.mark.asyncio
async def test_pruefer_sieht_quellenschluessel_statt_zitat_ids() -> None:
    import re

    fake = _Fake(_autor(), _sauber_pruefer())
    await _ausformulieren(fake)
    payload = fake.invocations[1].input_payload
    keys = {r["key"] for r in payload["source_refs"]}
    aufgabe = payload["aufgabe"]
    gesehen = list(aufgabe["citation_ids"])
    for claim in aufgabe["lektion"]["claims"]:
        gesehen += claim["citation_ids"]
    assert gesehen and set(gesehen) <= keys
    uuid_form = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-")
    assert not [i for i in gesehen if uuid_form.match(i)]
    assert "zitate" not in aufgabe


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


_NUR_FUER_DEN_SERVER = {"chunk_id", "chunk_content_hash", "source_content_hash", "source_id", "source_revision", "locator"}


@pytest.mark.asyncio
async def test_das_modell_bekommt_je_quellstueck_nur_schluessel_ueberschrift_und_text() -> None:
    # Live 28.10.: 59 KB je Autor-Aufruf, davon 7,6 KB Quelltext; der Rest waren Hashes und IDs,
    # die das Modell nie braucht (es zitiert nur per S-Schluessel, der Server loest auf).
    import json

    refs = tuple(
        json.dumps({**json.loads(r), "content": f"Text {n}", "locator": {"heading": "Dateien", "line_start": n,
                                                                      "normalized_block_hash": "b" * 64}})
        for n, r in enumerate(REFS_2, start=1)
    )
    fake = _Fake(_autor(), _sauber_pruefer())
    erg = await _ausformulieren(fake, source_refs=refs)
    assert erg.zitate  # citations still resolve through the full catalog on the server
    for invocation in fake.invocations:
        for ref in invocation.input_payload["source_refs"]:
            assert not set(ref) & _NUR_FUER_DEN_SERVER, invocation.role
    autor = fake.invocations[0].input_payload["source_refs"]
    assert autor[0] == {"key": "S1", "heading": "Dateien", "content": "Text 1"}

    plan = _Fake(Grobentwurf.model_validate({
        "schema_version": "werkstatt-grobentwurf-v1", "title": "D", "audience": "E", "default_kind": "quiz",
        "chapters": [{"title": "K", "tasks": [{"summary": "s"}]}],
    }))
    await grobentwurf_erzeugen(plan, correlation_id=str(uuid4()), title="D", audience="E", outcomes=["o"],
                               default_kind="quiz", hinweise=[], source_refs=refs)
    assert plan.invocations[0].input_payload["source_refs"][1] == {"key": "S2", "heading": "Dateien", "content": "Text 2"}


@pytest.mark.asyncio
async def test_pruefung_laeuft_neben_der_event_loop(monkeypatch) -> None:
    # A number task's check starts a child process; it must not block the loop.
    import threading

    from spaces.learning.services.werkstatt import autor

    echt = autor.aufgabe_pruefen
    threads: list[threading.Thread] = []

    def merken(*args, **kwargs):
        threads.append(threading.current_thread())
        return echt(*args, **kwargs)

    monkeypatch.setattr(autor, "aufgabe_pruefen", merken)
    await _ausformulieren(_Fake(_autor(), _sauber_pruefer()))
    assert threads and all(t is not threading.main_thread() for t in threads)


# Mathe roles (Task 6): own role texts for `fach == "mathe"`, terminal texts byte-identical.

# sha256 of the three terminal role texts as they were before the mathe roles (28.10.).
_TERMINAL_TEXT_SHA256 = {
    "werkstatt_grobentwurf": "9d32489556d92a95087a97a6ce1fdfe4c788e61dae3aeb547135b1891afc4bff",
    "werkstatt_aufgabe": "04397d992124034a18f7bfe4c97992bdbbdca1cc74e215f084f0c2c67cae89ed",
    "werkstatt_pruefer": "43e26cd016e41119add118618c42fdd7ce7ce825179d9887509eeec514e92641",
}
_TERMINAL_VERTRAEGE = {
    "werkstatt_grobentwurf": ("werkstatt", "werkstatt-grobentwurf-prompt-v1", "werkstatt-grobentwurf-v1"),
    "werkstatt_aufgabe": ("werkstatt", "werkstatt-aufgabe-prompt-v2", "werkstatt-aufgabe-v1"),
    "werkstatt_pruefer": ("werkstatt", "werkstatt-pruefer-prompt-v1", "werkstatt-pruefer-v1"),
}
_MATHE_KURS = {"course_id": "c1", "title": "Kurvendiskussion", "audience": "Oberstufe", "fach": "mathe"}


def _mathe_text(role: str) -> str:
    from spaces.learning.services.course_factory.model_gateway import ROLE_PROMPTS

    return ROLE_PROMPTS[role]


def test_terminal_rollentexte_sind_byte_gleich() -> None:
    import hashlib

    from spaces.learning.services.course_factory.model_gateway import ROLE_CONTRACTS, ROLE_PROMPTS

    for role, sha in _TERMINAL_TEXT_SHA256.items():
        assert hashlib.sha256(ROLE_PROMPTS[role].encode("utf-8")).hexdigest() == sha, role
        assert ROLE_CONTRACTS[role] == _TERMINAL_VERTRAEGE[role], role


def test_mathe_rollen_haben_eigene_prompt_version_und_dasselbe_schema() -> None:
    from spaces.learning.services.course_factory.model_gateway import ROLE_CONTRACTS, ROLE_PROMPTS

    for basis, (stage, _prompt, schema) in _TERMINAL_VERTRAEGE.items():
        mathe = f"{basis}_mathe"
        name = basis.replace("_", "-")
        assert ROLE_CONTRACTS[mathe] == (stage, f"{name}-mathe-prompt-v1", schema), mathe
        assert ROLE_PROMPTS[mathe] and ROLE_PROMPTS[mathe] != ROLE_PROMPTS[basis]


@pytest.mark.asyncio
async def test_mathe_kurs_nutzt_mathe_rollen() -> None:
    fake = _Fake(_autor(), _sauber_pruefer())
    await _ausformulieren(fake, kurs=_MATHE_KURS)
    assert fake.calls == ["werkstatt_aufgabe_mathe", "werkstatt_pruefer_mathe"]
    assert [i.prompt_version for i in fake.invocations] == [
        "werkstatt-aufgabe-mathe-prompt-v1", "werkstatt-pruefer-mathe-prompt-v1",
    ]


@pytest.mark.asyncio
async def test_mathe_reparatur_bleibt_in_der_mathe_rolle() -> None:
    fake = _Fake(_schlecht(), _autor(), _sauber_pruefer())
    await _ausformulieren(fake, kurs=_MATHE_KURS)
    assert fake.calls == ["werkstatt_aufgabe_mathe", "werkstatt_aufgabe_mathe", "werkstatt_pruefer_mathe"]


@pytest.mark.asyncio
async def test_terminal_kurs_nutzt_die_bisherigen_rollen() -> None:
    fake = _Fake(_autor(), _sauber_pruefer())
    await _ausformulieren(fake, kurs={**_MATHE_KURS, "fach": "terminal"})
    assert fake.calls == ["werkstatt_aufgabe", "werkstatt_pruefer"]


@pytest.mark.asyncio
async def test_grobentwurf_waehlt_die_rolle_nach_fach() -> None:
    def entwurf() -> Grobentwurf:
        return Grobentwurf.model_validate({
            "schema_version": "werkstatt-grobentwurf-v1", "title": "K", "audience": "O", "default_kind": "number",
            "chapters": [{"title": "K", "tasks": [{"summary": "s"}]}],
        })

    args = dict(correlation_id=str(uuid4()), title="K", audience="O", outcomes=["o"], default_kind="number",
                hinweise=[], source_refs=REFS_2)
    mathe, terminal, ohne = _Fake(entwurf()), _Fake(entwurf()), _Fake(entwurf())
    await grobentwurf_erzeugen(mathe, fach="mathe", **args)
    await grobentwurf_erzeugen(terminal, fach="terminal", **args)
    await grobentwurf_erzeugen(ohne, **args)
    assert mathe.calls == ["werkstatt_grobentwurf_mathe"]
    assert mathe.invocations[0].prompt_version == "werkstatt-grobentwurf-mathe-prompt-v1"
    assert terminal.calls == ohne.calls == ["werkstatt_grobentwurf"]


def _formel_kaputt() -> AufgabeAusgabe:
    return _autor(prompt="Welcher Wert ergibt sich fuer $f(2)?")


@pytest.mark.asyncio
async def test_mathe_pruefung_meldet_formel_ungueltig() -> None:
    # aufgabe_pruefen runs with the course's fach: the mathe rule finds the open "$".
    fake = _Fake(_formel_kaputt(), _formel_kaputt(), _sauber_pruefer())
    erg = await _ausformulieren(fake, kurs=_MATHE_KURS)
    assert "formel_ungueltig" in codes(erg.befunde)
    assert any("Formel" in t for t in fake.invocations[1].input_payload["reviewer_feedback"])

    terminal = _Fake(_formel_kaputt(), _sauber_pruefer())
    erg = await _ausformulieren(terminal, kurs={**_MATHE_KURS, "fach": "terminal"})
    assert "formel_ungueltig" not in codes(erg.befunde)


@pytest.mark.parametrize(
    ("role", "model", "content"),
    [
        ("werkstatt_aufgabe_mathe", "AufgabeAusgabe", {"schema_version": "werkstatt-aufgabe-v1"}),
        ("werkstatt_pruefer_mathe", "PrueferAusgabe", {"schema_version": "werkstatt-pruefer-v1", "issues": []}),
    ],
)
def test_echte_validierung_nimmt_mathe_rollen_an(role: str, model: str, content: dict) -> None:
    from spaces.learning.services.werkstatt import autor

    if model == "AufgabeAusgabe":
        content = {**content, "aufgabe": quiz()}
    ergebnis = _durch_echte_validierung(role, getattr(autor, model), content)
    assert ergebnis.output.schema_version == content["schema_version"]


# Every blocking check must be avoidable from the author text alone: phrases per check.
_STEUERUNG = {
    "formel_ungueltig": ["$…$", "$$…$$", "KaTeX", "Close every $ and every $$", "no LaTeX commands except"],
    "luecke_mit_term": ["only numbers", "single words", "never a term", "a point such as"],
    "zahl_falsch": ["exactly one value", "`rechnung`", "exactly that value"],
    "zahl_unpruefbar": ["SymPy", "variable x", "power of a power", "at most 30 digits", "N(…) only as a function", "never a comma"],
    "graph_beziehung_falsch": ["`beziehung`", "ableitung_von", "derivative on the right"],
    "graph_ungueltig": ["`left_graph`", "`right_graph`", "visible inside the window"],
    "matching_duplicate": ["look different"],
    "skizze_rubrik": ["3 to 5 rubric criteria", "approximate coordinates"],
    "skizze_ohne_quelle": ["at least one source key"],
    "claim_uncited": ["each with at least one source key", "worked example", "keys that appear in `source_refs`"],
    "quellenschluessel_im_text": ["never write source keys"],
    "lesson_contains_tasks": ["Lösung:", "no tasks, options or answers"],
    "lektion_kuendigt_an": ["Im Folgenden", "In diesem Abschnitt", "In dieser Lektion"],
    "cloze_blank_missing": ["{{id}} placeholder", "exactly one blank"],
    "address_mixed": ["'Sie'"],
    "kind_abweichend": ["of the given kind"],
}


@pytest.mark.parametrize("code", sorted(_STEUERUNG))
def test_mathe_autortext_steuert_jede_sperrende_pruefung(code: str) -> None:
    text = _mathe_text("werkstatt_aufgabe_mathe")
    fehlt = [p for p in _STEUERUNG[code] if p not in text]
    assert not fehlt, (code, fehlt)


def test_mathe_autortext_hat_die_pflichtinhalte() -> None:
    text = _mathe_text("werkstatt_aufgabe_mathe")
    for pflicht in (
        "calculate", "read off a graph", "decide", "find the mistake", "never ask the learner to recite",
        '"x ="', "neutral label", "polynomial functions of degree 2 to 4", "simple rational functions",
        "German",
    ):
        assert pflicht in text, pflicht


def test_mathe_texte_nennen_nur_erlaubte_namen_und_befehle() -> None:
    import re

    from spaces.learning.services.werkstatt.formeln import ERLAUBTE_BEFEHLE
    from spaces.learning.services.werkstatt.graph import GRAPH_NAMEN
    from spaces.learning.services.werkstatt.nachrechnen import ERLAUBTE_NAMEN

    text = _mathe_text("werkstatt_aufgabe_mathe")
    befehle = set(re.findall(r"\\([A-Za-z]+)", text))
    assert befehle and befehle <= ERLAUBTE_BEFEHLE, befehle - ERLAUBTE_BEFEHLE
    rechnung = re.search(r"Calculation names: ([^.]+)\.", text).group(1)
    assert set(rechnung.replace(" ", "").split(",")) == ERLAUBTE_NAMEN - {"x", "subs", "N"}
    graph = re.search(r"Graph term names: ([^.]+)\.", text).group(1)
    assert set(graph.replace(" ", "").split(",")) == GRAPH_NAMEN - {"x"}


def test_mathe_beispielrechnungen_im_autortext_rechnen_nach() -> None:
    from spaces.learning.services.werkstatt.formeln import formel_fehler
    from spaces.learning.services.werkstatt.nachrechnen import nachrechnen

    text = _mathe_text("werkstatt_aufgabe_mathe")
    # The sketch example belongs to the same f(x) = x^3 - 3x: high point (-1|2), low point (1|-2).
    assert "Hochpunkt bei etwa (-1|2)" in text and "Tiefpunkt bei etwa (1|-2)" in text
    for rechnung, wert in (("solve(diff(x**3-3*x, x), x)[1]", 1.0), ("(x**2-4).subs(x, 3)", 5.0)):
        assert f"`{rechnung}`" in text
        assert nachrechnen(rechnung) == pytest.approx(wert)
    # The text's own formula examples must pass the formula check it steers towards.
    for beispiel in (r"$f'(x) = 3x^2 - 3$", r"$\frac{2x+1}{x-1}$"):
        assert beispiel in text and formel_fehler(beispiel) == [], beispiel


def test_mathe_grobentwurf_und_pruefer_texte() -> None:
    plan = _mathe_text("werkstatt_grobentwurf_mathe")
    for pflicht in ("calculate", "never recite", "degree 2 to 4", "simple rational functions", "'Sie'",
                    "reviewer_feedback", "$…$"):
        assert pflicht in plan, pflicht
    pruefer = _mathe_text("werkstatt_pruefer_mathe")
    for pflicht in ("Recompute", "every result", "contradict", "Do not rewrite", "German sentence"):
        assert pflicht in pruefer, pflicht
