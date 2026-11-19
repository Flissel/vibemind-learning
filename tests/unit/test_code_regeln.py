"""Regelliste je Kurs: was Programme der Lernenden ohne Frage, mit Frage oder nie duerfen."""

from __future__ import annotations

from typing import Any

import pytest

from spaces.learning.services.evaluation.code_tests import Testfall, fingerabdruck, regeln_lesen
from spaces.learning.services.werkstatt.freigabe import freigabe_ausfuehren
from spaces.learning.services.werkstatt.inhalt import inhalt_lesen
from spaces.learning.services.werkstatt.katalog import _item_felder
from spaces.learning.tests.unit.test_werkstatt_freigabe import (
    COURSE,
    _aufgabe,
    _befund,
    _code,
    _freigeben,
    _quiz,
    _store,
    _vorbereiten,
)
from spaces.learning.tests.unit.test_werkstatt_freigabe_ausfuehren import FakeGateway


def test_regeln_lesen_behaelt_nur_bekannte_abweichungen_in_fester_reihenfolge() -> None:
    assert regeln_lesen(None) == {}
    assert regeln_lesen({"daten": "nie", "pakete": "ohne_frage", "diagramm": "mit_frage"}) == {
        "pakete": "ohne_frage", "daten": "nie",
    }
    assert list(regeln_lesen({"daten": "nie", "pakete": "ohne_frage"})) == ["pakete", "daten"]
    # Unbekanntes faellt weg: eine alte oder fremde Zeile darf nichts oeffnen.
    assert regeln_lesen({"netz": "ohne_frage", "pakete": "immer", "diagramm": 1}) == {}
    assert regeln_lesen(["pakete"]) == {}


def test_kurs_traegt_die_regeln(tmp_path) -> None:
    s = _store(tmp_path)
    assert s.kurs(COURSE)["code_regeln"] == {}
    s.kurs_aendern(COURSE, code_regeln={"pakete": "ohne_frage", "daten": "nie", "diagramm": "mit_frage"})
    assert s.kurs(COURSE)["code_regeln"] == {"pakete": "ohne_frage", "daten": "nie"}
    # Ohne Angabe bleibt die Liste, mit leerer Angabe gilt wieder ueberall "mit Frage".
    s.kurs_aendern(COURSE, title="Neu")
    assert s.kurs(COURSE)["code_regeln"] == {"pakete": "ohne_frage", "daten": "nie"}
    s.kurs_aendern(COURSE, code_regeln={})
    assert s.kurs(COURSE)["code_regeln"] == {}


def test_geaenderte_regeln_sind_ein_eigener_freigabe_vorschlag(tmp_path) -> None:
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    _freigeben(s, _vorbereiten(s)["freigabe_id"])
    assert _vorbereiten(s)["nichts_zu_veroeffentlichen"] is True

    s.kurs_aendern(COURSE, code_regeln={"pakete": "ohne_frage"})
    r = _vorbereiten(s)
    assert r["nichts_zu_veroeffentlichen"] is False and r["blockiert"] is False
    assert r["vergleich"]["code_regeln"] == {"pakete": "ohne_frage"}
    assert r["vergleich"]["code_regeln_vorher"] == {}
    _freigeben(s, r["freigabe_id"])

    # Dieselben Regeln noch einmal: nichts zu veroeffentlichen, kein Vorher-Eintrag.
    wieder = _vorbereiten(s)
    assert wieder["nichts_zu_veroeffentlichen"] is True
    assert wieder["vergleich"]["code_regeln"] == {"pakete": "ohne_frage"}
    assert "code_regeln_vorher" not in wieder["vergleich"]

    # Zurueck auf den Grundzustand ist wieder eine Aenderung.
    s.kurs_aendern(COURSE, code_regeln={})
    zurueck = _vorbereiten(s)
    assert zurueck["nichts_zu_veroeffentlichen"] is False
    assert zurueck["vergleich"]["code_regeln_vorher"] == {"pakete": "ohne_frage"}
    assert "code_regeln" not in zurueck["vergleich"]


def test_kurs_ohne_regeln_traegt_keinen_schluessel(tmp_path) -> None:
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    vergleich = _vorbereiten(s)["vergleich"]
    assert "code_regeln" not in vergleich and "code_regeln_vorher" not in vergleich


def _code_mit(erlaubnisse: list[str]) -> dict[str, Any]:
    c = _code(erlaubnisse=erlaubnisse)
    lauf = {
        "fingerabdruck": fingerabdruck(
            c["sprache"], c["musterloesung"], [Testfall(**t) for t in c["tests"]], erlaubnisse=erlaubnisse),
        "bestanden": True, "fehlgeschlagen": [],
    }
    return {**c, "testlauf": lauf}


def test_aufgabe_die_eine_verbotene_erlaubnis_vorsieht_sperrt_die_freigabe(tmp_path) -> None:
    s = _store(tmp_path)
    t1 = _aufgabe(s, _code_mit(["pakete", "diagramm"]), findings=[])
    assert _vorbereiten(s)["blockiert"] is False

    s.kurs_aendern(COURSE, code_regeln={"diagramm": "nie", "pakete": "ohne_frage"})
    r = _vorbereiten(s)
    assert r["blockiert"] is True
    (b,) = _befund(r, "aufgabe_mit_befunden")
    assert b["task_id"] == t1 and "ein Diagramm ausgeben" in b["text"] and "Pakete" not in b["text"]


@pytest.mark.asyncio
async def test_freigabe_liefert_die_gezeigten_regeln_aus_nicht_die_spaeteren(tmp_path) -> None:
    class Katalog:
        def __init__(self) -> None:
            self.aufrufe: list[dict[str, Any]] = []

        def letzte_revision(self, course_id: str, *, unter: int) -> int | None:
            return None

        def hat_revision(self, course_id: str, revision: int) -> bool:
            return False

        def projizieren(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.aufrufe.append(kwargs)
            return {"items": 1, "konzepte": 1, "lernstand_uebernommen": 0}

    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    erste = _vorbereiten(s)
    await freigabe_ausfuehren(s, FakeGateway(), erste["freigabe_id"], approval_ref="k1", learnhouse_revision=1)

    s.kurs_aendern(COURSE, code_regeln={"pakete": "ohne_frage"})
    v = _vorbereiten(s, revision=2)
    # Nach dem Anzeigen geaendert: ausgeliefert wird, was die Inhaberin gesehen hat.
    s.kurs_aendern(COURSE, code_regeln={"pakete": "nie"})
    katalog = Katalog()
    r = await freigabe_ausfuehren(
        s, FakeGateway(), v["freigabe_id"], approval_ref="k2", learnhouse_revision=2, katalog=katalog)

    assert r["learnhouse_revision"] == 3
    assert katalog.aufrufe[0]["code_regeln"] == {"pakete": "ohne_frage"}


def test_item_nennt_was_ohne_frage_gilt_und_laesst_verbotenes_weg() -> None:
    inhalt = inhalt_lesen(_code_mit(["pakete", "diagramm", "daten"]))
    _typ, _erwartet, config, _rubrik = _item_felder(
        "item-1", inhalt, code_regeln={"pakete": "ohne_frage", "diagramm": "nie", "lange_laufzeit": "ohne_frage"})
    darstellung = config["presentation"]
    assert darstellung["erlaubnisse"] == ["pakete", "daten"]
    # Nur was die Aufgabe selbst vorsieht: die Kursregel hebt keine Obergrenze an.
    assert darstellung["ohne_frage"] == ["pakete"]

    ohne = _item_felder("item-1", inhalt)[2]["presentation"]
    assert ohne["erlaubnisse"] == ["pakete", "diagramm", "daten"] and "ohne_frage" not in ohne
