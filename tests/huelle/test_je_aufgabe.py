"""Ein Canvas je Aufgabe: neue Solo-Listensitzungen (Merkmal in `je-aufgabe.json`) bekommen je Aufgabe ein eigenes
PenEcho-Dokument unter dem Schluessel `<sid>#<nummer>`; Sitzungen ohne Merkmal laufen wie bisher auf einem Board."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from spaces.learning.huelle.anordnung import EINZEL_RECHTECK, ZuVieleAufgaben, anordnung
from spaces.learning.huelle.bruecke import _Sitzung
from spaces.learning.huelle.einstellung import Kurs
from spaces.learning.huelle.penecho_mcp import PenEchoNichtErreichbar, aufgaben_titel

from .test_bruecke import (
    GID, KURS, PA, SID, SID_GA, FakePenEcho, Laeufe, ListenLern, TabMcp, _bis, _gruppen_bruecke, _stand,
    _tab_penecho, _ueberblick, bauen, gruppen_lern_kennung,
)


class JePenEcho(FakePenEcho):
    """Wie FakePenEcho, aber jede Aufgaben-Sitzung (`<sid>#<n>`) hat ihr eigenes Board `board-<n>`; nach
    `vergessen` ein neues (`board-<n>-v1`, ...). `fehler_nach_vergessen`: das naechste `sitzung` danach scheitert."""

    def __init__(self) -> None:
        super().__init__()
        self.vergessen_je: dict[str, int] = {}
        self.fehler_nach_vergessen: Exception | None = None
        self._gerade_vergessen = False

    def vergessen(self, schluessel: str) -> None:
        super().vergessen(schluessel)
        self.vergessen_je[schluessel] = self.vergessen_je.get(schluessel, 0) + 1
        self._gerade_vergessen = True

    def sitzung(self, schluessel: str, titel: str, canvas_id: str | None = None, gruppe: str | None = None) -> str:
        board = super().sitzung(schluessel, titel, canvas_id, gruppe)
        if self._gerade_vergessen:
            self._gerade_vergessen = False
            if self.fehler_nach_vergessen is not None:
                fehler, self.fehler_nach_vergessen = self.fehler_nach_vergessen, None
                raise fehler
        if "#" not in schluessel:
            return board
        stand = self.vergessen_je.get(schluessel, 0)
        return f"board-{schluessel.split('#', 1)[1]}" + (f"-v{stand}" if stand else "")


def _neu_lern(n: int = 3) -> ListenLern:
    """Listenkurs ohne laufende Sitzung: das erste Oeffnen startet einen neuen Durchlauf (Zweig mit Merkmal)."""
    ll = ListenLern(n)
    ll.lern.antworten["learning_course_overview"] = [lambda kw: _ueberblick(None), lambda kw: _ueberblick(ll.turn())]
    ll.lern.antworten["learning_session_start"] = lambda kw: {"session": ll.turn()}
    return ll


def _oeffnen(ll: ListenLern, tmp_path: Path, penecho: FakePenEcho | None = None
             ) -> tuple[TestClient, FakePenEcho, dict[str, Any]]:
    client, _, penecho = bauen(ll.lern, penecho or JePenEcho(), kursende=tmp_path / "kursende.json")
    r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    assert r.status_code == 200, r.text
    _bis(lambda: client.app._flaechenspur.wartend(f"{SID}:setzen:") == 0, 2)
    return client, penecho, r.json()


def _schluessel(penecho: FakePenEcho) -> list[str]:
    return [a[1] for a in penecho.aufrufe if a[0] in ("sitzung", "vergessen")]


def test_neue_sitzung_bekommt_merkmal(tmp_path: Path) -> None:
    client, _, offen = _oeffnen(_neu_lern(), tmp_path)
    try:
        assert json.loads((tmp_path / "je-aufgabe.json").read_text(encoding="utf-8")) == [SID]
        assert client.app._sitzungen[SID].je_aufgabe is True
        assert _stand(client)["je_aufgabe"] is True
        assert offen["je_aufgabe"] is True and offen["stand"]["je_aufgabe"] is True
    finally:
        client.app.beenden()


def test_oeffnen_setzt_nur_erste_aufgabe_eigenes_board(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    client, penecho, offen = _oeffnen(ll, tmp_path)
    try:
        assert offen["nummer"] == 1 and offen["flaeche"] is True
        sitzungen = [a for a in penecho.aufrufe if a[0] == "sitzung"]
        assert sitzungen and all(a[1:3] == (f"{SID}#1", "Aufgabe 1: Frage 1") for a in sitzungen)
        assert aufgaben_titel(1, "Frage 1") == "Aufgabe 1: Frage 1"
        assert SID not in _schluessel(penecho)  # nie das gemeinsame Board
        assert ("bereiche_melden", "board-1", [EINZEL_RECHTECK]) in penecho.aufrufe
        task_1 = ll.eintraege[0]["task"]["id"]
        assert [a for a in penecho.aufrufe if a[0] == "aufgabe_setzen"] == [
            ("aufgabe_setzen", "board-1", task_1, "Frage 1", 1)]
        assert penecho.rechtecke == [(1, EINZEL_RECHTECK)] and penecho.zeigen == [(1, True)]
        namen = penecho.namen()
        assert namen.index("bereiche_melden") < namen.index("aufgabe_setzen") < namen.index("dokument_merken")
        assert ("dokument_merken", "board-1") in penecho.aufrufe
        # Keine Hintergrund-Arbeit fuer die uebrigen Aufgaben.
        assert client.app._flaechenspur.zustand(f"{SID}:setzen:2") is None
        assert client.app._sitzungen[SID].aufgaben_boards == {1: "board-1"}
        assert [a["nummer"] for a in offen["aufgaben"]] == [1, 2, 3]
    finally:
        client.app.beenden()


def test_oeffnen_ohne_anordnung_auch_bei_vielen_aufgaben(tmp_path: Path) -> None:
    # Im alten Modell 409 `zu_viele_aufgaben`; je Aufgabe ein Canvas braucht keine gemeinsame Anordnung.
    with pytest.raises(ZuVieleAufgaben):
        anordnung([False] * 45)
    client, penecho, offen = _oeffnen(_neu_lern(45), tmp_path)
    try:
        assert offen["flaeche"] is True and len(offen["aufgaben"]) == 45
        assert penecho.namen().count("aufgabe_setzen") == 1
    finally:
        client.app.beenden()


def test_zeigen_legt_canvas_bei_bedarf_an(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    penecho = JePenEcho()
    penecho.dokumente_je_schluessel[f"{SID}#3"] = "doc-3"  # Aufgabe 3 hat schon ein Dokument (vor einem Neustart)
    client, _, _ = _oeffnen(ll, tmp_path, penecho)
    try:
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert r.status_code == 200, r.text
        assert r.json() == {"nummer": 2, "flaeche": True}
        neu = penecho.aufrufe[vorher:]
        assert ("sitzung", f"{SID}#2", "Aufgabe 2: Frage 2") in neu
        assert ("aufgabe_setzen", "board-2", ll.eintraege[1]["task"]["id"], "Frage 2", 2) in neu
        assert ("bereiche_melden", "board-2", [EINZEL_RECHTECK]) in neu
        assert penecho.rechtecke[-1] == (2, EINZEL_RECHTECK) and penecho.zeigen[-1] == (2, True)
        assert client.app._sitzungen[SID].aufgaben_boards == {1: "board-1", 2: "board-2"}

        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert r.status_code == 200 and r.json() == {"nummer": 2, "flaeche": True}
        neu = penecho.aufrufe[vorher:]
        assert "aufgabe_setzen" not in [a[0] for a in neu]
        assert ("bereich_zeigen", "board-2", EINZEL_RECHTECK) in neu

        # Gemerktes Dokument ohne Board in diesem Prozess: nur zeigen (PenEcho holt das Dokument zurueck).
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 3})
        assert r.status_code == 200 and r.json() == {"nummer": 3, "flaeche": True}
        neu = penecho.aufrufe[vorher:]
        assert "aufgabe_setzen" not in [a[0] for a in neu]
        assert ("bereich_zeigen", "board-3", EINZEL_RECHTECK) in neu
        assert SID not in _schluessel(penecho)
    finally:
        client.app.beenden()


def test_aufgaben_canvas_beruehrt_gemeinsames_board_nie(tmp_path: Path) -> None:
    # `sitzung.board` bleibt das gemeinsame Board des alten Modells; Aufgaben-Canvas stehen nur in `aufgaben_boards`.
    client, penecho, _ = _oeffnen(_neu_lern(3), tmp_path)
    try:
        sitzung = client.app._sitzungen[SID]
        assert sitzung.board is None
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2}).status_code == 200
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 1}).status_code == 200
        assert sitzung.board is None
        assert sitzung.aufgaben_boards == {1: "board-1", 2: "board-2"}
        assert [a for a in penecho.aufrufe if a[0] == "dokument_merken"] == [
            ("dokument_merken", "board-1"), ("dokument_merken", "board-2")]
    finally:
        client.app.beenden()


def test_unbekannte_aufgaben_sitzung_verwirft_altes_board(tmp_path: Path) -> None:
    penecho = JePenEcho()
    client, _, _ = _oeffnen(_neu_lern(3), tmp_path, penecho)
    try:
        sitzung = client.app._sitzungen[SID]
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2}).status_code == 200
        assert sitzung.aufgaben_boards[2] == "board-2"
        # PenEcho kennt die Sitzung nicht mehr: vergessen, neu anlegen, wiederholen.
        penecho.unbekannt_einmal = True
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert r.json() == {"nummer": 2, "flaeche": True}
        assert ("vergessen", f"{SID}#2") in penecho.aufrufe
        assert penecho.aufrufe[-1] == ("bereich_zeigen", "board-2-v1", EINZEL_RECHTECK)
        assert sitzung.aufgaben_boards[2] == "board-2-v1" and sitzung.board is None
        # Scheitert auch das Neuanlegen, bleibt kein veraltetes Board stehen.
        penecho.unbekannt_einmal = True
        penecho.fehler_nach_vergessen = PenEchoNichtErreichbar("weg")
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert r.json() == {"nummer": 2, "flaeche": False}
        assert 2 not in sitzung.aufgaben_boards and sitzung.board is None
    finally:
        client.app.beenden()


def test_alte_sitzung_bleibt_ein_board(tmp_path: Path) -> None:
    # Laufende Sitzung ohne Merkmal (eine andere Sitzung steht in der Datei): ein Board, Bereiche aus `anordnung`.
    (tmp_path / "je-aufgabe.json").write_text(json.dumps(["andere-sitzung"]), encoding="utf-8")
    client, penecho, offen = _oeffnen(ListenLern(3), tmp_path, FakePenEcho())
    try:
        bereiche = anordnung([False] * 3)
        assert set(_schluessel(penecho)) == {SID}
        assert ("bereiche_melden", "pe-1", bereiche) in penecho.aufrufe
        assert ("bereich_zeigen", "pe-1", bereiche[0]) in penecho.aufrufe
        assert sorted(n for n, _ in penecho.rechtecke) == [1, 2, 3]  # die uebrigen im Hintergrund wie bisher
        assert "dokument_von" not in penecho.namen()
        assert client.app._sitzungen[SID].je_aufgabe is False
        assert offen["je_aufgabe"] is False and offen["dokumente"] == {}
        stand = _stand(client)
        assert stand["je_aufgabe"] is False and stand["dokumente"] == {}
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert r.status_code == 200
        assert penecho.aufrufe[-1] == ("bereich_zeigen", "pe-1", bereiche[1])
        assert json.loads((tmp_path / "je-aufgabe.json").read_text(encoding="utf-8")) == ["andere-sitzung"]
    finally:
        client.app.beenden()


def test_neustart_bleibt_je_aufgabe(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    client, _, _ = _oeffnen(ll, tmp_path)
    client.app.beenden()
    # Zweite Bruecke auf denselben Dateien; Learning nennt jetzt die laufende Sitzung.
    penecho = JePenEcho()
    penecho.dokumente_je_schluessel[f"{SID}#1"] = "doc-1"
    client, penecho, offen = _oeffnen(ll, tmp_path, penecho)
    try:
        assert offen["je_aufgabe"] is True and offen["dokumente"] == {"doc-1": 1}
        assert ("sitzung", f"{SID}#1", "Aufgabe 1: Frage 1") in penecho.aufrufe
        assert SID not in _schluessel(penecho)
    finally:
        client.app.beenden()
    # Dritte Bruecke: zuerst eine Sitzungsroute (Wiederaufbau ueber `_sitzung`), kein Oeffnen.
    client, _, _ = bauen(ll.lern, JePenEcho(), kursende=tmp_path / "kursende.json")
    try:
        assert _stand(client)["je_aufgabe"] is True
        assert client.app._sitzungen[SID].je_aufgabe is True
    finally:
        client.app.beenden()


def test_merkmal_beim_wiederaufbau_in_frei(tmp_path: Path) -> None:
    (tmp_path / "je-aufgabe.json").write_text(json.dumps([SID]), encoding="utf-8")
    client, _, _ = bauen(ListenLern(3).lern, JePenEcho(), kursende=tmp_path / "kursende.json")
    bruecke = client.app
    try:
        andere = _Sitzung(Kurs(course_id="11111111-1111-4111-8111-111111111112", titel="Anderer"),
                          "88888888-8888-4888-8888-888888888888", "lh-1-test")
        bruecke._frei(andere)  # fragt den Ueberblick des Kurses und merkt dessen laufende Sitzung
        assert bruecke._sitzungen[SID].je_aufgabe is True
    finally:
        bruecke.beenden()


def test_gruppe_nie_je_aufgabe(tmp_path: Path) -> None:
    laeufe = Laeufe({gruppen_lern_kennung(PA, GID): SID_GA})
    client, penecho, _, _ = _gruppen_bruecke(laeufe, kursende=tmp_path / "kursende.json")
    try:
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        datei = tmp_path / "je-aufgabe.json"
        assert not datei.exists() or SID_GA not in json.loads(datei.read_text(encoding="utf-8"))
        assert client.app._sitzungen[SID_GA].je_aufgabe is False
        assert all("#" not in k for k in _schluessel(penecho))
    finally:
        client.app.beenden()


def test_eigene_dokumente_enthaelt_aufgaben(tmp_path: Path) -> None:
    penecho = _tab_penecho(tmp_path, TabMcp())
    client, _, _ = bauen(ListenLern(3).lern, penecho, kursende=tmp_path / "kursende.json")  # type: ignore[arg-type]
    bruecke = client.app
    try:
        bruecke._sitzungen[SID] = _Sitzung(Kurs(course_id=KURS, titel="Testkurs"), SID, PA, je_aufgabe=True)
        penecho._merken({f"{SID}#1": "doc-1", f"{SID}#2": "doc-2", "fremd#1": "doc-x"}, penecho._dokumentdatei)
        assert bruecke.eigene_dokumente(PA) == frozenset({"doc-1", "doc-2"})
    finally:
        bruecke.beenden()


def test_stand_dokumentkarte(tmp_path: Path) -> None:
    penecho = JePenEcho()
    penecho.dokumente_je_schluessel[f"{SID}#1"] = "doc-1"
    client, _, offen = _oeffnen(_neu_lern(3), tmp_path, penecho)
    try:
        assert _stand(client)["dokumente"] == {"doc-1": 1}
        assert offen["dokumente"] == {"doc-1": 1} and offen["stand"]["dokumente"] == {"doc-1": 1}
    finally:
        client.app.beenden()
