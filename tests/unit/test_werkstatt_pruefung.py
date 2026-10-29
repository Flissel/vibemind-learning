"""Werkstatt: deterministic checks of one task, no model."""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from spaces.learning.services.course_factory.verification import ChunkEvidence, SourceSnapshot
from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
from spaces.learning.services.werkstatt.pruefung import aufgabe_pruefen, quellen_aufloesen

_SRC_ID = "00000000-0000-4000-8000-0000000000a1"
_CHUNK_1 = "00000000-0000-4000-8000-0000000000b1"
_CHUNK_2 = "00000000-0000-4000-8000-0000000000b2"
_HASH = "a" * 64
_KOMMANDO = "Test-Path C:/Daten/bericht.txt"

SRC = (
    SourceSnapshot(
        source_id=_SRC_ID,
        revision=1,
        content_hash=_HASH,
        chunks=(
            ChunkEvidence(_CHUNK_1, _HASH, {"page": 1}, f"Mit {_KOMMANDO} pruefen Sie, ob eine Datei existiert."),
            ChunkEvidence(_CHUNK_2, _HASH, {"page": 2}, "Weitere Hinweise."),
        ),
    ),
)
REFS_2 = tuple(
    json.dumps(
        {
            "source_id": _SRC_ID,
            "source_revision": 1,
            "chunk_id": chunk,
            "chunk_content_hash": _HASH,
            "locator": {"page": number},
        }
    )
    for number, chunk in enumerate((_CHUNK_1, _CHUNK_2), start=1)
)

_TEXT = "Der Befehl Test-Path prueft, ob eine Datei oder ein Ordner existiert."


def lektion(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Dateien pruefen",
        "body": f"{_TEXT}\n\n```powershell\n{_KOMMANDO}\n```\n",
        "claims": [{"claim_id": "c1", "text": _TEXT, "citation_ids": ["S1"]}],
        "concept_ids": ["k1"],
    }
    base.update(over)
    return base


def quiz(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "quiz",
        "prompt": "Welcher Befehl prueft, ob eine Datei existiert?",
        "concept_ids": ["k1"],
        "citation_ids": ["S1"],
        "options": [
            {"option_id": "a", "text": "Test-Path"},
            {"option_id": "b", "text": "Get-Date"},
            {"option_id": "c", "text": "Get-Help"},
        ],
        "correct_option_ids": ["a"],
        "lektion": lektion(),
    }
    base.update(over)
    return base


def gut() -> Any:
    inhalt, _ = quellen_aufloesen(inhalt_lesen(quiz()), REFS_2, course_id="c1")
    return inhalt


def codes(befunde: list[dict[str, str]]) -> list[str]:
    return [b["code"] for b in befunde]


def test_quiz_alle_bis_auf_eine_ist_befund() -> None:
    inhalt = inhalt_lesen(
        quiz(
            options=[{"option_id": x, "text": f"Option {x}"} for x in "abcd"],
            correct_option_ids=["a", "b", "c"],
        )
    )
    inhalt, _ = quellen_aufloesen(inhalt, REFS_2, course_id="c1")
    assert codes(aufgabe_pruefen(inhalt, sources=SRC)) == ["quiz_all_but_one"]


def test_lektion_mit_aufgabe_im_text_ist_befund() -> None:
    body = f"{_TEXT}\n\nAufgabe 1: Pruefen Sie eine Datei.\n\n```powershell\n{_KOMMANDO}\n```\n"
    inhalt, _ = quellen_aufloesen(inhalt_lesen(quiz(lektion=lektion(body=body))), REFS_2, course_id="c1")
    assert "lesson_contains_tasks" in codes(aufgabe_pruefen(inhalt, sources=SRC))


def test_du_anrede_ist_befund() -> None:
    inhalt, _ = quellen_aufloesen(
        inhalt_lesen(quiz(prompt="Wie pruefst du, ob eine Datei existiert?")),
        REFS_2,
        course_id="c1",
    )
    body = f"{_TEXT} Danach koennen Sie weiterarbeiten.\n\n```powershell\n{_KOMMANDO}\n```\n"
    inhalt = inhalt.model_copy(update={"lektion": inhalt.lektion.model_copy(update={"body": body})})
    assert "address_mixed" in codes(aufgabe_pruefen(inhalt, sources=SRC))


def test_unbekannter_quellenschluessel_ist_befund() -> None:
    mit_claim = inhalt_lesen(
        quiz(
            citation_ids=["S9"],
            lektion=lektion(claims=[{"claim_id": "c1", "text": _TEXT, "citation_ids": ["S9"]}]),
        )
    )
    inhalt, zitate = quellen_aufloesen(mit_claim, REFS_2, course_id="c1")
    assert zitate == []
    assert inhalt.citation_ids == [] and inhalt.zitate == []
    assert "claim_uncited" in codes(aufgabe_pruefen(inhalt, sources=SRC))


def test_befehl_nicht_in_quelle_ist_befund() -> None:
    body = f"{_TEXT}\n\n```powershell\nTest-Path C:/Anderes/ding.txt\n```\n"
    inhalt, _ = quellen_aufloesen(inhalt_lesen(quiz(lektion=lektion(body=body))), REFS_2, course_id="c1")
    assert codes(aufgabe_pruefen(inhalt, sources=SRC)) == ["command_not_in_source"]


def test_quellenschluessel_werden_zu_zitaten() -> None:
    inhalt, zitate = quellen_aufloesen(inhalt_lesen(quiz()), REFS_2, course_id="c1")
    erwartet = str(uuid5(NAMESPACE_URL, f"c1:{_CHUNK_1}"))
    assert [z.citation_id for z in zitate] == [erwartet]
    assert zitate[0].chunk_id == _CHUNK_1 and zitate[0].content_hash == _HASH
    assert inhalt.zitate == zitate
    assert inhalt.citation_ids == [erwartet]
    assert inhalt.lektion.claims[0].citation_ids == [erwartet]


def test_luecke_ohne_inhalt_und_doppelte_eintraege_sind_befunde() -> None:
    cloze = inhalt_lesen(
        {
            "kind": "cloze",
            "prompt": "Ergaenzen Sie den Befehl.",
            "concept_ids": ["k1"],
            "template": "{{a}} C:/x",
            "blanks": [{"blank_id": "a", "accepted": ["  "]}],
        }
    )
    assert "cloze_blank_empty" in codes(aufgabe_pruefen(cloze, sources=SRC))
    ordering = inhalt_lesen(
        {"kind": "ordering", "prompt": "Ordnen Sie.", "concept_ids": ["k1"], "items": ["a", "b", "a"]}
    )
    assert codes(aufgabe_pruefen(ordering, sources=SRC)) == ["ordering_duplicate"]
    matching = inhalt_lesen(
        {
            "kind": "matching",
            "prompt": "Ordnen Sie zu.",
            "concept_ids": ["k1"],
            "pairs": [
                {"left": "x", "right": "1"},
                {"left": "x", "right": "2"},
                {"left": "y", "right": "3"},
            ],
        }
    )
    assert codes(aufgabe_pruefen(matching, sources=SRC)) == ["matching_duplicate"]


def test_offene_aufgabe_ohne_musterantwort_ist_befund() -> None:
    offen = inhalt_lesen(
        {
            "kind": "open",
            "prompt": "Erklaeren Sie Test-Path.",
            "concept_ids": ["k1"],
            "expected_answer": " ",
            "rubric": [{"criterion_id": "r1", "description": "Nennt den Zweck", "points": 2}],
        }
    )
    assert codes(aufgabe_pruefen(offen, sources=SRC)) == ["expected_answer_missing"]


def test_saubere_aufgabe_hat_keine_befunde() -> None:
    assert aufgabe_pruefen(gut(), sources=SRC) == []


def test_jeder_befund_hat_deutschen_text() -> None:
    schlecht = inhalt_lesen(
        quiz(
            prompt="Wie pruefst du das?",
            options=[{"option_id": x, "text": f"Option {x}"} for x in "abcd"],
            correct_option_ids=["a", "b", "c"],
            lektion=lektion(
                body=f"Aufgabe 1: Los. Danach koennen Sie das.\n\n```powershell\nTest-Path C:/Fremd\n```\n",
                claims=[{"claim_id": "c1", "text": _TEXT, "citation_ids": []}],
            ),
        )
    )
    befunde = aufgabe_pruefen(schlecht, sources=SRC)
    assert len(befunde) >= 4
    for b in befunde:
        assert b["code"] and len(b["text"]) > 20 and b["text"].rstrip().endswith((".", "!", "?"))
        assert b["code"] not in b["text"]


def _refs(revision: int) -> tuple[str, ...]:
    return tuple(
        json.dumps(
            {
                "source_id": _SRC_ID,
                "source_revision": revision,
                "chunk_id": chunk,
                "chunk_content_hash": _HASH,
                "locator": {"page": number},
            }
        )
        for number, chunk in enumerate((_CHUNK_1, _CHUNK_2), start=1)
    )


_FREMD_ID = "00000000-0000-4000-8000-0000000000c1"


def _zitat(chunk: str, *, revision: int = 1, citation_id: str = _FREMD_ID) -> dict[str, Any]:
    return {
        "citation_id": citation_id, "source_id": _SRC_ID, "source_revision": revision,
        "chunk_id": chunk, "content_hash": _HASH, "locator": {"page": 1},
    }


def _mit_zitat(zitat: dict[str, Any]) -> Any:
    cid = zitat["citation_id"]
    return inhalt_lesen(
        quiz(
            citation_ids=[cid],
            zitate=[zitat],
            lektion=lektion(claims=[{"claim_id": "c1", "text": _TEXT, "citation_ids": [cid]}]),
        )
    )


def test_erfundenes_zitat_ist_unbelegt() -> None:
    erfunden = _zitat("00000000-0000-4000-8000-0000000000f9")  # chunk not in the catalog
    inhalt, zitate = quellen_aufloesen(_mit_zitat(erfunden), REFS_2, course_id="c1")
    assert zitate == [] and inhalt.zitate == []
    assert inhalt.citation_ids == [] and inhalt.lektion.claims[0].citation_ids == []
    assert "claim_uncited" in codes(aufgabe_pruefen(inhalt, sources=SRC))


def test_zitat_auf_alte_revision_ist_unbelegt() -> None:
    alt = _zitat(_CHUNK_1, revision=1)
    inhalt, zitate = quellen_aufloesen(_mit_zitat(alt), _refs(2), course_id="c1")
    assert zitate == [] and inhalt.lektion.claims[0].citation_ids == []
    assert "claim_uncited" in codes(aufgabe_pruefen(inhalt, sources=SRC))


def test_zitat_mit_anderem_inhaltshash_ist_unbelegt() -> None:
    falsch = {**_zitat(_CHUNK_1), "content_hash": "b" * 64}
    inhalt, zitate = quellen_aufloesen(_mit_zitat(falsch), REFS_2, course_id="c1")
    assert zitate == [] and inhalt.lektion.claims[0].citation_ids == []


def test_bekanntes_zitat_der_aktuellen_quelle_bleibt() -> None:
    # e.g. a takeover citation: its id is not ours, but it points at the current chunk.
    bekannt = _zitat(_CHUNK_1)
    inhalt, zitate = quellen_aufloesen(_mit_zitat(bekannt), REFS_2, course_id="c1")
    assert [z.citation_id for z in zitate] == [_FREMD_ID]
    assert inhalt.lektion.claims[0].citation_ids == [_FREMD_ID]
    assert "claim_uncited" not in codes(aufgabe_pruefen(inhalt, sources=SRC))


def test_aussage_mit_unaufgeloestem_schluessel_ist_unbelegt() -> None:
    roh = inhalt_lesen(quiz())  # claims cite "S1", never resolved against the catalog
    assert "claim_uncited" in codes(aufgabe_pruefen(roh, sources=SRC))


def _mit_body(body: str) -> Any:
    inhalt, _ = quellen_aufloesen(inhalt_lesen(quiz(lektion=lektion(body=body))), REFS_2, course_id="c1")
    return inhalt


def test_quellenschluessel_im_text_ist_befund() -> None:
    body = f"Geben Sie den folgenden Befehl ein (S37):\n\n```powershell\n{_KOMMANDO}\n```\n{_TEXT}"
    assert "quellenschluessel_im_text" in codes(aufgabe_pruefen(_mit_body(body), sources=SRC))
    prompt = inhalt_lesen(quiz(prompt="Welcher Befehl prueft eine Datei [S2]?"))
    prompt, _ = quellen_aufloesen(prompt, REFS_2, course_id="c1")
    assert "quellenschluessel_im_text" in codes(aufgabe_pruefen(prompt, sources=SRC))


def test_schluesselmuster_in_woertern_und_code_ist_kein_befund() -> None:
    body = f"{_TEXT} Die Zeile S3 steht im Wort BuS3 und in Seite (S. 3).\n\n```powershell\n{_KOMMANDO} # (S1)\n```\n"
    assert "quellenschluessel_im_text" not in codes(aufgabe_pruefen(_mit_body(body), sources=SRC))


def test_codeblock_ohne_shell_ist_befund() -> None:
    for tag in ("text", ""):
        body = f"{_TEXT}\n\n```{tag}\n{_KOMMANDO}\n```\n"
        assert "codeblock_ohne_shell" in codes(aufgabe_pruefen(_mit_body(body), sources=SRC))
    for tag in ("cmd", "bash", "PowerShell"):
        body = f"{_TEXT}\n\n```{tag}\n{_KOMMANDO}\n```\n"
        assert "codeblock_ohne_shell" not in codes(aufgabe_pruefen(_mit_body(body), sources=SRC))


def test_lektion_kuendigt_an_ist_befund() -> None:
    for satz in ("In diesem Abschnitt lernen Sie den Befehl kennen.", "Im Folgenden sehen Sie ihn."):
        body = f"{satz} {_TEXT}\n\n```powershell\n{_KOMMANDO}\n```\n"
        assert "lektion_kuendigt_an" in codes(aufgabe_pruefen(_mit_body(body), sources=SRC))
    body = f"{_TEXT} In diesem Abschnitt lernen Sie mehr.\n\n```powershell\n{_KOMMANDO}\n```\n"
    assert "lektion_kuendigt_an" in codes(aufgabe_pruefen(_mit_body(body), sources=SRC))


def test_saubere_lektion_hat_keinen_der_neuen_befunde() -> None:
    neu = {"quellenschluessel_im_text", "codeblock_ohne_shell", "lektion_kuendigt_an"}
    assert not neu & set(codes(aufgabe_pruefen(gut(), sources=SRC)))


def _mathe_offen(**over: Any) -> Any:
    base: dict[str, Any] = {
        "kind": "open", "prompt": "Berechnen Sie die Ableitung.", "expected_answer": "3",
        "rubric": [{"criterion_id": "r1", "description": "Ableitung korrekt", "points": 2}],
        "concept_ids": ["k1"], "citation_ids": [],
    }
    base.update(over)
    return inhalt_lesen(base)


def test_mathe_ohne_shell_regel() -> None:
    body = "Die Ableitung von $x^2$ ist $2x$." + chr(10)*2 + "```text" + chr(10) + "f(x)=x^2" + chr(10) + "```" + chr(10)
    inhalt = inhalt_lesen(quiz(lektion=lektion(body=body, claims=[])))
    assert "codeblock_ohne_shell" in codes(aufgabe_pruefen(inhalt, sources=SRC))
    assert "codeblock_ohne_shell" not in codes(aufgabe_pruefen(inhalt, sources=SRC, fach="mathe"))
    assert "command_not_in_source" not in codes(aufgabe_pruefen(inhalt, sources=SRC, fach="mathe"))


def test_mathe_formel_ungueltig() -> None:
    inhalt = _mathe_offen(prompt="Berechnen Sie $f(2$")
    assert "formel_ungueltig" in codes(aufgabe_pruefen(inhalt, sources=SRC, fach="mathe"))
    assert "formel_ungueltig" not in codes(aufgabe_pruefen(inhalt, sources=SRC))
    ok = _mathe_offen(prompt="Berechnen Sie $f(2)$")
    assert "formel_ungueltig" not in codes(aufgabe_pruefen(ok, sources=SRC, fach="mathe"))


def _luecke(accepted: list[str]) -> Any:
    return inhalt_lesen({
        "kind": "cloze", "prompt": "Ergaenzen Sie.", "template": "Ergebnis: {{b1}}",
        "blanks": [{"blank_id": "b1", "accepted": accepted}],
        "concept_ids": ["k1"], "citation_ids": [],
    })


def test_mathe_luecke_mit_term() -> None:
    def c(accepted: list[str]) -> list[str]:
        return codes(aufgabe_pruefen(_luecke(accepted), sources=SRC, fach="mathe"))

    assert "luecke_mit_term" in c(["3x^2-3"])
    assert "luecke_mit_term" in c(["x=2"])
    assert "luecke_mit_term" in c(["1/2"])
    assert "luecke_mit_term" not in c(["x-Achse"])
    assert "luecke_mit_term" not in c(["Sattel-Punkt"])
    assert "luecke_mit_term" not in c(["2"])
    assert "luecke_mit_term" not in c(["-1,5"])
    assert "luecke_mit_term" not in c(["Hochpunkt"])
    assert "luecke_mit_term" not in codes(aufgabe_pruefen(_luecke(["3x^2-3"]), sources=SRC))


def test_terminal_befunde_unveraendert() -> None:
    faelle = [
        gut(),
        inhalt_lesen(quiz(options=[{"option_id": x, "text": f"O {x}"} for x in "abcd"],
                          correct_option_ids=["a", "b", "c"])),
        inhalt_lesen(quiz(lektion=lektion(body="```text" + chr(10) + "x" + chr(10) + "```" + chr(10) + "Foo $5 und $6.", claims=[]))),
        _luecke(["3x^2-3"]),
    ]
    erwartet = [[], ["quiz_all_but_one", "claim_uncited"], ["codeblock_ohne_shell"], []]
    for f, soll in zip(faelle, erwartet, strict=True):
        assert codes(aufgabe_pruefen(f, sources=SRC)) == soll
        assert codes(aufgabe_pruefen(f, sources=SRC, fach="terminal")) == soll
    assert "formel_ungueltig" not in codes(aufgabe_pruefen(faelle[2], sources=SRC))


def _zahl(**over: Any) -> Any:
    base: dict[str, Any] = {
        "kind": "number", "prompt": "Berechnen Sie $1+1$.", "value": 2, "rechnung": "1+1",
        "concept_ids": ["k1"], "citation_ids": [],
    }
    base.update(over)
    return inhalt_lesen(base)


def test_zahl_wird_nachgerechnet() -> None:
    for fach in ("terminal", "mathe"):
        assert codes(aufgabe_pruefen(_zahl(), sources=SRC, fach=fach)) == []
        assert codes(aufgabe_pruefen(_zahl(value=3), sources=SRC, fach=fach)) == ["zahl_falsch"]
        assert codes(aufgabe_pruefen(_zahl(rechnung="open(1)"), sources=SRC, fach=fach)) == ["zahl_unpruefbar"]
    # Within the tolerance is no finding.
    assert codes(aufgabe_pruefen(_zahl(value=2.005), sources=SRC)) == []
    assert codes(aufgabe_pruefen(_zahl(value=2.4, tolerance=0.5), sources=SRC)) == []
    assert codes(aufgabe_pruefen(_zahl(value=2.02), sources=SRC)) == ["zahl_falsch"]
    befund = aufgabe_pruefen(_zahl(rechnung="__import__('os')"), sources=SRC)
    assert befund[0]["code"] == "zahl_unpruefbar" and "nachrechnen" in befund[0]["text"]
    # The repair round needs to know what is allowed.
    assert all(n in befund[0]["text"] for n in ("solve", "diff", "subs", "N(", "30 Stellen"))


def _graph_zuordnung(rechts_erstes: str = "3*x**2", links_erstes: str = "x**3", **over: Any) -> Any:
    base: dict[str, Any] = {
        "kind": "matching", "prompt": "Ordnen Sie jedem Graphen den Graphen seiner Ableitung zu.",
        "concept_ids": ["k1"], "citation_ids": [], "beziehung": "ableitung_von",
        "pairs": [
            {"left": "f", "right": "f'", "left_graph": {"term": links_erstes}, "right_graph": {"term": rechts_erstes}},
            {"left": "g", "right": "g'", "left_graph": {"term": "sin(x)"}, "right_graph": {"term": "cos(x)"}},
            {"left": "h", "right": "h'", "left_graph": {"term": "exp(x)"}, "right_graph": {"term": "exp(x)"}},
        ],
    }
    base.update(over)
    return inhalt_lesen(base)


def test_graph_beziehung_wird_geprueft() -> None:
    for fach in ("terminal", "mathe"):
        assert codes(aufgabe_pruefen(_graph_zuordnung(), sources=SRC, fach=fach)) == []
        assert codes(aufgabe_pruefen(_graph_zuordnung("2*x"), sources=SRC, fach=fach)) == ["graph_beziehung_falsch"]
    gleich = _graph_zuordnung("(x-1)*(x+1)", "x^2-1", beziehung="gleich")
    assert codes(aufgabe_pruefen(gleich, sources=SRC)) == ["graph_beziehung_falsch"]
    ohne = _graph_zuordnung("2*x", beziehung=None)
    assert codes(aufgabe_pruefen(ohne, sources=SRC)) == []


def test_graph_ungueltig_ist_befund() -> None:
    befunde = aufgabe_pruefen(_graph_zuordnung(links_erstes="__import__('os')"), sources=SRC, fach="mathe")
    assert codes(befunde) == ["graph_ungueltig"]
    assert all(n in befunde[0]["text"] for n in ("sqrt", "sin", "Abs")) and befunde[0]["text"].endswith(".")
    nichts = _graph_zuordnung(links_erstes="sqrt(x-10)", beziehung=None)
    assert codes(aufgabe_pruefen(nichts, sources=SRC)) == ["graph_ungueltig"]


def test_gleiche_graphen_auf_einer_seite_sind_doppelt() -> None:
    doppelt = _graph_zuordnung("cos(x)", "sin(x)", beziehung=None)
    assert codes(aufgabe_pruefen(doppelt, sources=SRC)) == ["matching_duplicate"]


def test_gleich_aussehende_graphen_sind_doppelt() -> None:
    # Learners see pictures: `2*x` and `x+x` are the same image, so the task is ambiguous.
    gleich_bild = inhalt_lesen({
        "kind": "matching", "prompt": "Ordnen Sie jedem Graphen den Graphen seiner Ableitung zu.",
        "concept_ids": ["k1"], "citation_ids": [], "beziehung": "ableitung_von",
        "pairs": [
            {"left": "f", "right": "f'", "left_graph": {"term": "x**2"}, "right_graph": {"term": "2*x"}},
            {"left": "g", "right": "g'", "left_graph": {"term": "x**2+1"}, "right_graph": {"term": "x+x"}},
            {"left": "h", "right": "h'", "left_graph": {"term": "sin(x)"}, "right_graph": {"term": "cos(x)"}},
        ],
    })
    assert codes(aufgabe_pruefen(gleich_bild, sources=SRC, fach="mathe")) == ["matching_duplicate"]
    # Other spellings of the same term, and terms that differ only outside the shown window.
    for links_a, links_b in (({"term": "x^3"}, {"term": "x**3"}), ({"term": "x", "x_min": 0}, {"term": "Abs(x)", "x_min": 0})):
        inhalt = inhalt_lesen({
            "kind": "matching", "prompt": "Ordnen Sie zu.", "concept_ids": ["k1"], "citation_ids": [],
            "pairs": [
                {"left": "f", "right": "eins", "left_graph": links_a},
                {"left": "g", "right": "zwei", "left_graph": links_b},
                {"left": "h", "right": "drei", "left_graph": {"term": "sin(x)"}},
            ],
        })
        assert codes(aufgabe_pruefen(inhalt, sources=SRC)) == ["matching_duplicate"]

def _skizze(**over: Any) -> Any:
    base: dict[str, Any] = {
        "kind": "sketch", "prompt": "Skizzieren Sie $f(x)=x^2$.", "concept_ids": ["k1"], "citation_ids": ["c1"],
        "expected_answer": "Nach oben offene Parabel.",
        "rubric": [
            {"criterion_id": "r1", "description": "Parabel nach oben offen", "points": 2},
            {"criterion_id": "r2", "description": "Scheitel im Ursprung", "points": 2},
            {"criterion_id": "r3", "description": "Achsen beschriftet", "points": 1},
        ],
    }
    base.update(over)
    return inhalt_lesen(base)


def test_skizze_pruefung() -> None:
    from spaces.learning.services.werkstatt.pruefung import _aufgabe_codes

    assert codes(aufgabe_pruefen(_skizze(), sources=SRC, fach="mathe")) == []
    assert "formel_ungueltig" in codes(aufgabe_pruefen(_skizze(prompt="Skizzieren Sie $f(2$"), sources=SRC, fach="mathe"))
    # The schema already demands 3; the finding guards a task built around it.
    zu_wenig = _skizze().model_copy(update={"rubric": _skizze().rubric[:2]})
    assert "skizze_rubrik" in _aufgabe_codes(zu_wenig)
    assert _aufgabe_codes(_skizze()) == []
    # The canvas evaluator needs a source reference: without a citation the task could not open.
    assert "skizze_ohne_quelle" in _aufgabe_codes(_skizze(citation_ids=[]))
