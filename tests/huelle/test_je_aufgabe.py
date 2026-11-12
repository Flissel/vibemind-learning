"""Ein Canvas je Aufgabe: neue Solo-Listensitzungen (Merkmal in `je-aufgabe.json`) bekommen je Aufgabe ein eigenes
PenEcho-Dokument unter dem Schluessel `<sid>#<nummer>`; Sitzungen ohne Merkmal laufen wie bisher auf einem Board."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from spaces.learning.huelle.anordnung import EINZEL_RECHTECK, ZuVieleAufgaben, anordnung
from spaces.learning.huelle.bruecke import _Sitzung
from spaces.learning.huelle.einstellung import Kurs
from spaces.learning.huelle.lern_mcp import LearningAbgelehnt
from spaces.learning.huelle.penecho_mcp import PenEchoBeschaeftigt, PenEchoNichtErreichbar, aufgaben_titel
from spaces.learning.huelle.zusammenfassung import Kursrueckmeldung

from .test_bruecke import (
    ACTOR, GID, KA, KOPF, KURS, PA, PNG, SID, SID_GA, FakePenEcho, Laeufe, ListenLern, TabMcp, _aufrufe, _bis,
    _gruppen_bruecke, _karten,
    _ruhig, _stand, _status, _tab_penecho, _ueberblick, _vid, _vor, bauen, gruppen_lern_kennung,
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


def _neu_lern(n: int = 3, *, typ: str = "numeric") -> ListenLern:
    """Listenkurs ohne laufende Sitzung: das erste Oeffnen startet einen neuen Durchlauf (Zweig mit Merkmal)."""
    ll = ListenLern(n, typ=typ)
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


def _flaechenaufrufe(penecho: FakePenEcho, ab: int = 0) -> list[tuple[Any, ...]]:
    """Aufrufe ab `ab` ohne `dokument_von` (liest nur `dokumente.json`, fuer die Modellangaben der Antworten)."""
    return [a for a in penecho.aufrufe[ab:] if a[0] != "dokument_von"]


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
        assert _ohne_modell(r.json()) == {"nummer": 2, "flaeche": True}
        neu = penecho.aufrufe[vorher:]
        assert ("sitzung", f"{SID}#2", "Aufgabe 2: Frage 2") in neu
        assert ("aufgabe_setzen", "board-2", ll.eintraege[1]["task"]["id"], "Frage 2", 2) in neu
        assert ("bereiche_melden", "board-2", [EINZEL_RECHTECK]) in neu
        assert penecho.rechtecke[-1] == (2, EINZEL_RECHTECK) and penecho.zeigen[-1] == (2, True)
        assert client.app._sitzungen[SID].aufgaben_boards == {1: "board-1", 2: "board-2"}

        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert r.status_code == 200 and _ohne_modell(r.json()) == {"nummer": 2, "flaeche": True}
        neu = penecho.aufrufe[vorher:]
        assert "aufgabe_setzen" not in [a[0] for a in neu]
        assert ("bereich_zeigen", "board-2", EINZEL_RECHTECK) in neu

        # Gemerktes Dokument ohne Board in diesem Prozess: nur zeigen (PenEcho holt das Dokument zurueck).
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 3})
        assert r.status_code == 200 and _ohne_modell(r.json()) == {"nummer": 3, "flaeche": True}
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
        assert _ohne_modell(r.json()) == {"nummer": 2, "flaeche": True}
        assert ("vergessen", f"{SID}#2") in penecho.aufrufe
        assert _flaechenaufrufe(penecho)[-1] == ("bereich_zeigen", "board-2-v1", EINZEL_RECHTECK)
        assert sitzung.aufgaben_boards[2] == "board-2-v1" and sitzung.board is None
        # Scheitert auch das Neuanlegen, bleibt kein veraltetes Board stehen.
        penecho.unbekannt_einmal = True
        penecho.fehler_nach_vergessen = PenEchoNichtErreichbar("weg")
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert _ohne_modell(r.json()) == {"nummer": 2, "flaeche": False}
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


# ---- Task 7: Abgabe, Rueckmeldung, Korrektur, Passt, Antwort und Weiter auf dem Canvas der Aufgabe ----

def _aufnahme(tmp_path: Path, nummer: int) -> Path:
    return tmp_path / "aufnahmen" / SID / f"{nummer}.png"


def test_abgabe_nimmt_board_der_aufgabe(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    client, penecho, _ = _oeffnen(ll, tmp_path)
    bruecke = client.app
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2}).status_code == 200
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2})
        assert r.status_code == 202, r.text
        neu = penecho.aufrufe[vorher:]
        # Deterministischer Typ (numeric): nur das Ergebnisfeld, auf dem eigenen Canvas, Rechteck EINZEL_RECHTECK.
        assert ("aufgabe_aufnehmen", "board-2", EINZEL_RECHTECK, True) in neu
        assert not {"ergebnis_schnappschuss", "schnappschuss", "aufgabe_setzen"} & {a[0] for a in neu}
        assert _aufnahme(tmp_path, 2).read_bytes() == PNG
        assert bruecke._aufnahme_lesen(SID, 2) == PNG
        _bis(lambda: _ruhig(bruecke), 2)
        (karte,) = [k for k in _karten(penecho) if k[3] == 2]
        assert karte[1] == "board-2" and karte[4:6] == ("2", "Richtig.")
        assert penecho.karten_rechtecke[-1] == (2, EINZEL_RECHTECK)
        assert penecho.aufrufe[-1] == ("dokument_merken", "board-2")
        assert _status(_stand(client), 2) == "rueckmeldung"
        sitzung = bruecke._sitzungen[SID]
        assert sitzung.board is None and SID not in _schluessel(penecho)
        (kw,) = ll.vorschauen()
        assert set(kw["payload"]) == {"handschrift_png_base64", "attempt_id"}
    finally:
        bruecke.beenden()


def test_abgabe_rubrik_nimmt_ganzes_rechteck(tmp_path: Path) -> None:
    client, penecho, _ = _oeffnen(_neu_lern(2, typ="open"), tmp_path)
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1}).status_code == 202
        assert ("aufgabe_aufnehmen", "board-1", EINZEL_RECHTECK, False) in penecho.aufrufe
        _bis(lambda: _ruhig(client.app), 2)
    finally:
        client.app.beenden()


def test_abgabe_beschaeftigt_409(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    client, penecho, _ = _oeffnen(ll, tmp_path)
    bruecke = client.app
    try:
        penecho.aufnahme_fehler = lambda rechteck: PenEchoBeschaeftigt("CANVAS_BUSY")
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1})
        assert r.status_code == 409
        assert r.json() == {"fehler": "FLAECHE_BESCHAEFTIGT"}
        assert (SID, 1) not in bruecke._abgaben
        stand = _stand(client)
        assert _status(stand, 1) == "offen" and stand["wartend"] == 0
        assert ll.vorschauen() == [] and not _aufnahme(tmp_path, 1).exists()
        # Erneut abgeben geht, sobald die Flaeche frei ist.
        penecho.aufnahme_fehler = None
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1}).status_code == 202
        _bis(lambda: _ruhig(bruecke), 2)
        assert _status(_stand(client), 1) == "rueckmeldung"
    finally:
        bruecke.beenden()


def test_abgabe_ohne_canvas_legt_es_an(tmp_path: Path) -> None:
    # Aufgabe 3 war nie im Bild (kein Board, kein gemerktes Dokument): erst ihr Canvas setzen, dann aufnehmen.
    ll = _neu_lern(3)
    client, penecho, _ = _oeffnen(ll, tmp_path)
    try:
        vorher = len(penecho.aufrufe)
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 3}).status_code == 202
        namen = [a[0] for a in penecho.aufrufe[vorher:]]
        assert ("aufgabe_setzen", "board-3", ll.eintraege[2]["task"]["id"], "Frage 3", 3) in penecho.aufrufe
        assert namen.index("aufgabe_setzen") < namen.index("aufgabe_aufnehmen")
        _bis(lambda: _ruhig(client.app), 2)
    finally:
        client.app.beenden()


def test_abgabe_nach_neustart_nimmt_gemerktes_dokument(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    client, _, _ = _oeffnen(ll, tmp_path)
    client.app.beenden()
    penecho = JePenEcho()
    penecho.dokumente_je_schluessel[f"{SID}#2"] = "doc-2"
    client, _, _ = bauen(ll.lern, penecho, kursende=tmp_path / "kursende.json")
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2}).status_code == 202
        assert "aufgabe_setzen" not in penecho.namen()
        assert ("sitzung", f"{SID}#2", "Aufgabe 2: Frage 2") in penecho.aufrufe
        assert ("aufgabe_aufnehmen", "board-2", EINZEL_RECHTECK, True) in penecho.aufrufe
        _bis(lambda: _ruhig(client.app), 2)
        assert [k[1] for k in _karten(penecho)] == ["board-2"]
        assert SID not in _schluessel(penecho)
    finally:
        client.app.beenden()


def test_aufnahme_merken_ueberschreibt(tmp_path: Path) -> None:
    client, _, _ = bauen(ListenLern(1).lern, JePenEcho(), kursende=tmp_path / "kursende.json")
    bruecke = client.app
    try:
        assert bruecke._aufnahme_lesen(SID, 4) is None
        bruecke._aufnahme_merken(SID, 4, b"eins")
        bruecke._aufnahme_merken(SID, 4, b"zwei")
        assert bruecke._aufnahme_lesen(SID, 4) == b"zwei" and _aufnahme(tmp_path, 4).read_bytes() == b"zwei"
        assert [p.name for p in _aufnahme(tmp_path, 4).parent.iterdir()] == ["4.png"]  # keine Temp-Reste
    finally:
        bruecke.beenden()


def test_alte_sitzung_merkt_keine_aufnahme(tmp_path: Path) -> None:
    client, penecho, _ = _oeffnen(ListenLern(2), tmp_path, FakePenEcho())
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1}).status_code == 202
        assert ("ergebnis_schnappschuss", "pe-1", 1) in penecho.aufrufe
        assert "aufgabe_aufnehmen" not in penecho.namen()
        _bis(lambda: _ruhig(client.app), 2)
        assert not (tmp_path / "aufnahmen").exists()
    finally:
        client.app.beenden()


def test_korrektur_auf_canvas_der_aufgabe(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(2), gelesen="2")
    client, penecho, _ = _oeffnen(ll, tmp_path)
    try:
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur",
                        json={"nummer": 2, "vorbewertung_id": _vid(2), "answer": {"value": 2}})
        assert r.status_code == 200, r.text
        assert r.json()["flaeche"] is True
        neu = penecho.aufrufe[vorher:]
        assert [a[1] for a in neu if a[0] == "rueckmeldung_zeigen"] == ["board-2"]
        assert penecho.karten_rechtecke[-1] == (2, EINZEL_RECHTECK)
        assert ("dokument_merken", "board-2") in neu
        assert SID not in _schluessel(penecho)
    finally:
        client.app.beenden()


def test_passt_zeigt_naechste_aufgabe_auf_ihrem_canvas(tmp_path: Path) -> None:
    ll = _neu_lern(45)  # im alten Modell zu viele Aufgaben fuer eine Flaeche
    ll.eintraege[0]["vorbewertung"] = _vor(vid=_vid(1), gelesen="1")
    client, penecho, _ = _oeffnen(ll, tmp_path)
    try:
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": 1, "vorbewertung_id": _vid(1)})
        assert r.status_code == 200, r.text
        assert r.json()["weiter_zu"] == 2
        neu = penecho.aufrufe[vorher:]
        # Aufgabe 2 hatte noch kein Canvas: anlegen und zeigen.
        assert ("aufgabe_setzen", "board-2", ll.eintraege[1]["task"]["id"], "Frage 2", 2) in neu
        assert penecho.zeigen[-1] == (2, True)
        # Hat die naechste Aufgabe ihr Canvas schon, nur zeigen.
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 3}).status_code == 200
        ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(2), gelesen="2")
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": 2, "vorbewertung_id": _vid(2)})
        assert r.status_code == 200 and r.json()["weiter_zu"] == 3
        neu = _flaechenaufrufe(penecho, vorher)
        assert "aufgabe_setzen" not in [a[0] for a in neu]
        assert ("bereich_zeigen", "board-3", EINZEL_RECHTECK) in neu
        assert neu[-1] == ("dokument_merken", "board-3")
        assert SID not in _schluessel(penecho) and client.app._sitzungen[SID].board is None
    finally:
        client.app.beenden()


def test_antwort_rueckmeldung_auf_canvas_der_aufgabe(tmp_path: Path) -> None:
    ll = _neu_lern(3, typ="code")
    client, penecho, _ = _oeffnen(ll, tmp_path)
    try:
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"nummer": 1, "answer": {"code": "x"}})
        assert r.status_code == 200, r.text
        assert r.json()["flaeche"] is True and r.json()["weiter_zu"] == 2
        neu = penecho.aufrufe[vorher:]
        (setzen,) = [a for a in neu if a[0] == "rueckmeldung_setzen"]
        assert setzen[1] == "board-1" and setzen[4] == "Rückmeldung zu Aufgabe 1"
        assert penecho.rueckmeldung_rechtecke[-1] == (1, EINZEL_RECHTECK)
        assert neu.index(setzen) < neu.index(("dokument_merken", "board-1"))
        assert not {"aufgabe_setzen", "bereich_zeigen"} & {a[0] for a in neu}  # die Rueckmeldung bleibt im Bild
        assert SID not in _schluessel(penecho)
    finally:
        client.app.beenden()


def test_weiter_zeigt_aktuelle_aufgabe_auf_ihrem_canvas(tmp_path: Path) -> None:
    ll = _neu_lern(45)
    ll.eintraege[0]["status"] = "bestaetigt"
    client, penecho, offen = _oeffnen(ll, tmp_path)
    try:
        assert offen["nummer"] == 2
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
        assert r.status_code == 200, r.text
        assert r.json()["nummer"] == 2 and r.json()["flaeche"] is True
        assert _flaechenaufrufe(penecho, vorher) == [("sitzung", f"{SID}#2", "Aufgabe 2: Frage 2"),
                                            ("bereich_zeigen", "board-2", EINZEL_RECHTECK),
                                            ("dokument_merken", "board-2")]
    finally:
        client.app.beenden()


class MerkPenEcho(JePenEcho):
    """Wie JePenEcho, aber `dokument_merken` merkt wie PenEchoMcp das Dokument (`doc-<n>`) zum Schluessel `<sid>#<n>`
    des Boards: so taucht ein spaeter angelegtes Canvas in `dokument_von` (und damit in `dokumente`) auf."""

    def __init__(self) -> None:
        super().__init__()
        self._schluessel_von: dict[str, str] = {}

    def sitzung(self, schluessel: str, titel: str, canvas_id: str | None = None, gruppe: str | None = None) -> str:
        board = super().sitzung(schluessel, titel, canvas_id, gruppe)
        self._schluessel_von[board] = schluessel
        return board

    def dokument_merken(self, session_id: str) -> bool:
        ergebnis = super().dokument_merken(session_id)
        schluessel = self._schluessel_von.get(session_id)
        if schluessel is not None and "#" in schluessel:
            self.dokumente_je_schluessel[schluessel] = "doc-" + schluessel.split("#", 1)[1]
        return ergebnis


def _ohne_modell(antwort: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in antwort.items() if k not in ("je_aufgabe", "dokumente")}


def test_zeigen_neue_aufgabe_bringt_ihr_dokument(tmp_path: Path) -> None:
    # Die Seite ordnet PenEcho-Nachrichten ueber `dokumente` zu: ein erst jetzt angelegtes Canvas muss darin stehen.
    client, _, offen = _oeffnen(_neu_lern(3), tmp_path, MerkPenEcho())
    try:
        assert offen["dokumente"] == {"doc-1": 1}
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert r.status_code == 200, r.text
        assert r.json() == {"nummer": 2, "flaeche": True, "je_aufgabe": True, "dokumente": {"doc-1": 1, "doc-2": 2}}
    finally:
        client.app.beenden()


def test_passt_mit_weiter_zu_bringt_dessen_dokument(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    ll.eintraege[0]["vorbewertung"] = _vor(vid=_vid(1), gelesen="1")
    client, _, _ = _oeffnen(ll, tmp_path, MerkPenEcho())
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": 1, "vorbewertung_id": _vid(1)})
        assert r.status_code == 200, r.text
        assert r.json()["weiter_zu"] == 2
        assert r.json()["je_aufgabe"] is True and r.json()["dokumente"] == {"doc-1": 1, "doc-2": 2}
    finally:
        client.app.beenden()


def test_weiter_antwort_abgeben_korrektur_bringen_dokumente(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    ll.eintraege[0]["status"] = "bestaetigt"
    ll.eintraege[2]["vorbewertung"] = _vor(vid=_vid(3), gelesen="3")
    client, _, _ = _oeffnen(ll, tmp_path, MerkPenEcho())
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
        assert r.status_code == 200, r.text
        assert r.json()["je_aufgabe"] is True and r.json()["dokumente"] == {"doc-2": 2}
        r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur",
                        json={"nummer": 3, "vorbewertung_id": _vid(3), "answer": {"value": 3}})
        assert r.status_code == 200, r.text
        assert r.json()["je_aufgabe"] is True and r.json()["dokumente"] == {"doc-2": 2, "doc-3": 3}
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2})
        assert r.status_code == 202, r.text
        assert r.json()["je_aufgabe"] is True and r.json()["dokumente"] == {"doc-2": 2, "doc-3": 3}
        _bis(lambda: _ruhig(client.app), 2)
    finally:
        client.app.beenden()


def test_antwort_bringt_dokumente(tmp_path: Path) -> None:
    client, _, _ = _oeffnen(_neu_lern(3, typ="code"), tmp_path, MerkPenEcho())
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"nummer": 1, "answer": {"code": "x"}})
        assert r.status_code == 200, r.text
        assert r.json()["je_aufgabe"] is True and r.json()["dokumente"] == {"doc-1": 1}
    finally:
        client.app.beenden()


def test_alte_sitzung_antworten_ohne_modellangaben(tmp_path: Path) -> None:
    # Ohne Merkmal bleiben die Antworten von zeigen, weiter, passt und abgeben wie bisher (kein je_aufgabe/dokumente).
    (tmp_path / "je-aufgabe.json").write_text(json.dumps(["andere-sitzung"]), encoding="utf-8")
    ll = ListenLern(3)
    ll.eintraege[0]["vorbewertung"] = _vor(vid=_vid(1), gelesen="1")
    client, _, _ = _oeffnen(ll, tmp_path, FakePenEcho())
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2})
        assert r.status_code == 200 and r.json() == {"nummer": 2, "flaeche": True}
        for r in (client.post(f"/lernen/api/sitzungen/{SID}/weiter"),
                  client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": 1, "vorbewertung_id": _vid(1)}),
                  client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2})):
            assert r.status_code in (200, 202), r.text
            assert "je_aufgabe" not in r.json() and "dokumente" not in r.json()
        _bis(lambda: _ruhig(client.app), 2)
    finally:
        client.app.beenden()


def test_karte_kommt_mit_dem_canvas(tmp_path: Path) -> None:
    # Wie im alten Modell (Setzen samt Karte): bekommt eine offene Aufgabe mit Vorbewertung ein neues Canvas,
    # kommt ihre Karte mit; beim Oeffnen ebenso fuer die erste offene Aufgabe.
    ll = _neu_lern(3)
    ll.eintraege[0]["vorbewertung"] = _vor(vid=_vid(1), gelesen="1")
    ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(2), gelesen="2")
    client, penecho, _ = _oeffnen(ll, tmp_path)
    try:
        assert [(k[1], k[3]) for k in _karten(penecho)] == [("board-1", 1)]
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2}).status_code == 200
        assert [(k[1], k[3]) for k in _karten(penecho)] == [("board-1", 1), ("board-2", 2)]
        assert penecho.karten_rechtecke == [(1, EINZEL_RECHTECK), (2, EINZEL_RECHTECK)]
        # Auch ein vorhandenes Canvas bekommt die Karte wieder (sie ersetzt die vorige, steht also nie doppelt).
        vorher = len(penecho.aufrufe)
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2}).status_code == 200
        neu = [a[0] for a in _flaechenaufrufe(penecho, vorher) if a[0] != "sitzung"]
        assert neu == ["bereich_zeigen", "rueckmeldung_zeigen"]
        assert [(k[1], k[3]) for k in _karten(penecho)][-1] == ("board-2", 2)
        assert SID not in _schluessel(penecho)
    finally:
        client.app.beenden()


def test_verlorene_karte_kommt_auf_vorhandenes_canvas_zurueck(tmp_path: Path) -> None:
    # Die Karte fiel unterwegs weg (Kurswechsel, Neustart waehrend der Bewertung), Learning kennt die Vorbewertung:
    # Zeigen und Weiter auf ein vorhandenes Canvas setzen sie wieder; eine bestaetigte Aufgabe bekommt keine.
    ll = _neu_lern(3)
    client, penecho, _ = _oeffnen(ll, tmp_path)
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2}).status_code == 200
        assert _karten(penecho) == []
        ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(2), gelesen="2")
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 2}).status_code == 200
        assert [(k[1], k[3]) for k in _karten(penecho)] == [("board-2", 2)]
        assert penecho.karten_rechtecke[-1] == (2, EINZEL_RECHTECK)
        ll.eintraege[0]["vorbewertung"] = _vor(vid=_vid(1), gelesen="1")
        r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
        assert r.status_code == 200 and r.json()["nummer"] == 1
        assert [(k[1], k[3]) for k in _karten(penecho)][-1] == ("board-1", 1)
        ll.eintraege[2]["status"] = "bestaetigt"
        ll.eintraege[2]["vorbewertung"] = _vor(vid=_vid(3), gelesen="3")
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 3}).status_code == 200
        assert all(k[3] != 3 for k in _karten(penecho))
    finally:
        client.app.beenden()


def test_gescheiterte_abgabe_nimmt_ihre_aufnahme_zurueck(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    client, penecho, _ = _oeffnen(ll, tmp_path)
    bruecke = client.app
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1}).status_code == 202
        _bis(lambda: _ruhig(bruecke), 2)
        assert _aufnahme(tmp_path, 1).read_bytes() == PNG
        # Die naechste Abgabe scheitert nach dem Merken (Einreihen abgelehnt): die bewertete Aufnahme bleibt.
        penecho.bild = PNG[:8] + b"neu" * 8
        bruecke._modellspur.einreihen = lambda *a, **kw: False
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1})
        assert r.status_code == 409 and r.json()["code"] == "laeuft_schon"
        assert _aufnahme(tmp_path, 1).read_bytes() == PNG
        # Ohne vorige Aufnahme faellt die neue Datei weg.
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2})
        assert r.status_code == 409
        assert not _aufnahme(tmp_path, 2).exists()
        assert sorted(p.name for p in _aufnahme(tmp_path, 1).parent.iterdir()) == ["1.png"]
    finally:
        bruecke.beenden()


@pytest.mark.parametrize(("route", "body"), [
    ("abgeben", {"revision": 1}),
    ("korrektur", {"revision": 1, "vorbewertung_id": "55555555-5555-4555-8555-555555555601", "answer": {}}),
    ("passt", {"revision": 1, "vorbewertung_id": "55555555-5555-4555-8555-555555555601"}),
    ("antwort", {"revision": 1, "answer": {}}),
])
def test_alter_weg_ohne_nummer_nie_auf_gemeinsames_board(tmp_path: Path, route: str, body: dict[str, Any]) -> None:
    ll = _neu_lern(3)
    client, penecho, _ = _oeffnen(ll, tmp_path)
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/{route}", json=body)
        assert r.status_code == 409 and r.json()["code"] == "nicht_im_listenmodus"
        assert SID not in _schluessel(penecho)
        assert not {"learning_task_preview", "learning_task_confirm", "learning_task_answer"} & set(ll.lern.tools())
    finally:
        client.app.beenden()


# ---- Task 8: Kursrueckmeldung, Kursbericht, PDF und Aufraeumen am Kursende ------------------------------------

KI_ANTWORT = {"grader": "ki_flaeche", "staerken": "Sie rechnen sicher.", "luecken": "Einheiten fehlen.",
              "naechste_schritte": "Üben Sie Einheiten."}


class KursBewerter:
    """Bewerter mit Kursrueckmeldung (wie KiBewerter); `antwort` ist die Antwort der Flaechen-KI oder None.

    `halt`: die Anfrage wartet darauf (wie ein langsames Modell); `faeden` haelt fest, in welchem Faden sie lief.
    """

    aktiv = True

    def __init__(self, antwort: dict[str, Any] | None) -> None:
        self.antwort = antwort
        self.daten: list[dict[str, Any]] = []
        self.faeden: list[str] = []
        self.halt: threading.Event | None = None

    def bewerten(self, bild_png: bytes, daten: dict[str, Any]) -> None:
        return None

    def kursrueckmeldung(self, daten: dict[str, Any]) -> dict[str, Any] | None:
        self.faeden.append(threading.current_thread().name)
        if self.halt is not None:
            assert self.halt.wait(5), "Kursrueckmeldung nie freigegeben"
        self.daten.append(daten)
        return self.antwort

def _kurs_oeffnen(ll: ListenLern, tmp_path: Path, bewerter: Any = None, penecho: FakePenEcho | None = None
                  ) -> tuple[TestClient, FakePenEcho]:
    ll.lern.antworten["learning_task_bewertungsdaten"] = lambda kw: {}
    client, _, penecho = bauen(ll.lern, penecho or JePenEcho(), kursende=tmp_path / "kursende.json",
                               bewerter=bewerter)
    r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen", headers={KOPF: KA})
    assert r.status_code == 200, r.text
    return client, penecho


def _abgeben_passt(client: TestClient, nummer: int) -> dict[str, Any]:
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": nummer}, headers={KOPF: KA})
    assert r.status_code == 202, r.text
    _bis(lambda: _ruhig(client.app), 2)
    r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": nummer, "vorbewertung_id": _vid(nummer)},
                    headers={KOPF: KA})
    assert r.status_code == 200, r.text
    return r.json()


def _rueckmeldungen(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "kursrueckmeldungen.json").read_text(encoding="utf-8"))


FOLGT = "Die ausführliche Rückmeldung wird erstellt …"


def _kurs_ruhig(bruecke: Any) -> bool:
    """Keine Kursrueckmeldung mehr in der Modellspur und keine Kurskarte mehr in der Flaechenspur."""
    return bruecke._modellspur.wartend(f"{SID}:kurs") == 0 and bruecke._flaechenspur.wartend(f"{SID}:kurs") == 0


def _berichtstexte(penecho: FakePenEcho) -> list[str]:
    return [a[3] for a in penecho.aufrufe if a[0] == "bericht_zeigen"]


def test_kursbericht_ki_und_schliessen(tmp_path: Path) -> None:
    ll = _neu_lern(2)
    bewerter = KursBewerter(KI_ANTWORT)
    bewerter.halt = threading.Event()
    client, penecho = _kurs_oeffnen(ll, tmp_path, bewerter)
    bruecke = client.app
    try:
        _abgeben_passt(client, 1)
        vorher = len(penecho.aufrufe)
        _abgeben_passt(client, 2)  # Kursende: kehrt zurueck, waehrend die Flaechen-KI noch rechnet
        neu = penecho.aufrufe[vorher:]
        assert ("sitzung", f"{SID}#kurs", "Kursrückmeldung", KA) in neu
        (bericht,) = [a for a in neu if a[0] == "bericht_zeigen"]
        assert bericht[1:3] == ("board-kurs", SID) and bericht[4] == (1, 2)
        assert penecho.bericht_bilder[-1] == {1: PNG, 2: PNG} and penecho.bericht_rechtecke[-1] is None
        text = bericht[3]
        # Sofort: die Zahlen und der feste Hinweis, die Flaechen-KI wurde unter `_ablauf` nicht gefragt.
        assert text.startswith("# Bericht: ") and "## Kursrückmeldung" in text
        assert "**Stärken:** 1 von 2 Aufgaben richtig." in text and text.rstrip().endswith(FOLGT)
        assert bewerter.daten == [] and not (tmp_path / "kursrueckmeldungen.json").exists()
        assert ("bereich_zeigen", "board-kurs", EINZEL_RECHTECK) in neu
        assert ("dokument_merken", "board-kurs") in neu
        assert [a[1:] for a in neu if a[0] == "dokument_schliessen"] == [(f"{SID}#1", KA), (f"{SID}#2", KA)]
        # Nach dem Bericht holt nichts mehr ein Aufgaben-Canvas nach vorn.
        i = max(k for k, a in enumerate(neu) if a[0] == "bericht_zeigen")
        assert not [a for a in neu[i:] if a[0] in ("bereich_zeigen", "aufgabe_setzen")]
        assert bruecke._sitzungen[SID].bericht_gezeigt is True
        # Im Hintergrund: Antwort der Flaechen-KI, gespeichert, dieselbe Karte neu (ohne Hinweis, ohne Zeigen).
        vorher = len(penecho.aufrufe)
        bewerter.halt.set()
        _bis(lambda: _kurs_ruhig(bruecke), 2)
        assert bewerter.faeden == ["spur-modell"]
        neu = penecho.aufrufe[vorher:]
        (bericht,) = [a for a in neu if a[0] == "bericht_zeigen"]
        assert bericht[1:3] == ("board-kurs", SID)
        assert "**Stärken:** Sie rechnen sicher." in bericht[3]
        assert "**Nächste Schritte:** Üben Sie Einheiten." in bericht[3]
        assert FOLGT not in bericht[3] and "bereich_zeigen" not in [a[0] for a in neu]
        assert _rueckmeldungen(tmp_path)[SID] == {
            "staerken": "Sie rechnen sicher.", "luecken": "Einheiten fehlen.",
            "naechste_schritte": "Üben Sie Einheiten.", "quelle": "ki_flaeche"}
        (daten,) = bewerter.daten
        assert set(daten) == {"kurs", "aufgaben", "begriffe"}
        assert [a["nummer"] for a in daten["aufgaben"]] == [1, 2] and "musterloesung" not in repr(daten)
        assert "schnappschuss" not in penecho.namen() and "ergebnis_schnappschuss" not in penecho.namen()
        assert SID not in _schluessel(penecho) and bruecke._sitzungen[SID].board is None
        z = bruecke._zusammenfassung_bauen(bruecke._sitzungen[SID], bilder=False)
        assert z.rueckmeldung is not None and z.rueckmeldung.quelle == "ki_flaeche"
    finally:
        bruecke.beenden()


class KaputterBewerter(KursBewerter):
    def kursrueckmeldung(self, daten: dict[str, Any]) -> dict[str, Any] | None:
        super().kursrueckmeldung(daten)
        raise RuntimeError("weg")


@pytest.mark.parametrize("bewerter", [KursBewerter(None), KaputterBewerter(None), None],
                         ids=["endpunkt-none", "endpunkt-fehler", "ohne-bewerter"])
def test_kursbericht_ohne_endpunkt_und_ohne_schliessen(tmp_path: Path, bewerter: Any) -> None:
    ll = _neu_lern(2)
    penecho = JePenEcho()
    penecho.schliessen_ergebnis = False
    client, _ = _kurs_oeffnen(ll, tmp_path, bewerter, penecho)
    bruecke = client.app
    try:
        _abgeben_passt(client, 1)
        _abgeben_passt(client, 2)
        _bis(lambda: _kurs_ruhig(bruecke), 2)
        # Der Zahlen-Rueckfall wird nie gespeichert (ein spaeteres Oeffnen fragt die Flaechen-KI erneut).
        assert not (tmp_path / "kursrueckmeldungen.json").exists()
        assert SID in json.loads((tmp_path / "kursende.json").read_text(encoding="utf-8")).values()
        assert penecho.namen().count("dokument_schliessen") == 2
        texte = _berichtstexte(penecho)
        # BERICHT (Learning): Aufgabe 1 mit 1.0 richtig, Aufgabe 2 mit 0.7 nur teilweise.
        assert "**Stärken:** 1 von 2 Aufgaben richtig." in texte[-1] and "**Lücken:** Offen: Aufgabe 2." in texte[-1]
        assert FOLGT not in texte[-1]  # die Karte bleibt ohne Hinweis
        assert len(texte) == (1 if bewerter is None else 2)
        z = bruecke._zusammenfassung_bauen(bruecke._sitzungen[SID], bilder=False)
        assert z.rueckmeldung is not None and z.rueckmeldung.quelle == "zahlen"  # das PDF nimmt die Zahlen
    finally:
        bruecke.beenden()

def test_zusammenfassung_je_aufgabe_aus_aufnahmen_auch_ueber_zwanzig(tmp_path: Path) -> None:
    ll = _neu_lern(45)
    client, penecho, _ = _oeffnen(ll, tmp_path)
    bruecke = client.app
    try:
        bruecke._aufnahme_merken(SID, 1, PNG)
        bruecke._aufnahme_merken(SID, 30, PNG)
        bruecke._kursrueckmeldung_merken(SID, Kursrueckmeldung("S", "L", "N", "ki_flaeche"))
        assert _rueckmeldungen(tmp_path)[SID]["staerken"] == "S"
        vorher = len(penecho.aufrufe)
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert penecho.aufrufe[vorher:] == []  # keine Aufnahme, kein Board
        z = bruecke._zusammenfassung_bauen(bruecke._sitzungen[SID], bilder=True)
        assert [a.bild for a in z.aufgaben][:2] == [PNG, None] and z.aufgaben[29].bild == PNG
        assert z.rueckmeldung is not None and (z.rueckmeldung.staerken, z.rueckmeldung.quelle) == ("S", "ki_flaeche")
        assert bruecke._zusammenfassung_bauen(bruecke._sitzungen[SID], bilder=False).aufgaben[0].bild is None
    finally:
        bruecke.beenden()


def _beendet(n: int, tmp_path: Path) -> ListenLern:
    """Abgeschlossener Listenkurs mit Merkmal und gemerktem Kursende (wie nach einem Neustart der Huelle)."""
    ll = ListenLern(n)
    for i, e in enumerate(ll.eintraege, start=1):
        e["status"], e["vorbewertung"] = "bestaetigt", _vor(vid=_vid(i), gelesen=str(i))
    ll.lern.antworten["learning_course_overview"] = lambda kw: _ueberblick(None)
    (tmp_path / "kursende.json").write_text(json.dumps({f"{ACTOR}|{KURS}": SID}), encoding="utf-8")
    (tmp_path / "je-aufgabe.json").write_text(json.dumps([SID]), encoding="utf-8")
    return ll


def test_oeffnen_nach_kursende_zeigt_kursrueckmeldung(tmp_path: Path) -> None:
    ll = _beendet(45, tmp_path)
    bewerter = KursBewerter(KI_ANTWORT)
    client, penecho = _kurs_oeffnen(ll, tmp_path, bewerter)
    bruecke = client.app
    try:
        assert bruecke._sitzungen[SID].je_aufgabe is True
        assert ("sitzung", f"{SID}#kurs", "Kursrückmeldung", KA) in penecho.aufrufe
        _bis(lambda: _kurs_ruhig(bruecke), 2)
        assert [a[1] for a in penecho.aufrufe if a[0] == "bericht_zeigen"] == ["board-kurs", "board-kurs"]
        assert SID not in _schluessel(penecho) and len(bewerter.daten) == 1
        assert _rueckmeldungen(tmp_path)[SID]["quelle"] == "ki_flaeche"
        # Erneutes Oeffnen nach der KI-Antwort: das Kurs-Canvas nach vorn, kein zweiter Bericht, nichts eingereiht.
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen", headers={KOPF: KA})
        assert r.status_code == 200 and r.json()["abgeschlossen"] is True
        assert bruecke._modellspur.wartend(f"{SID}:kurs") == 0
        neu = penecho.aufrufe[vorher:]
        assert ("bereich_zeigen", "board-kurs", EINZEL_RECHTECK) in neu and "bericht_zeigen" not in [a[0] for a in neu]
        _bis(lambda: _kurs_ruhig(bruecke), 2)
        assert len(bewerter.daten) == 1
    finally:
        bruecke.beenden()
    # Nach einem Neustart: die gespeicherte Rueckmeldung gilt, die Flaechen-KI wird nicht erneut gefragt. Keine
    # gemeinsame Anordnung (45 Aufgaben, `_bereiche` wuerde `zu_viele_aufgaben` melden).
    bewerter2 = KursBewerter(KI_ANTWORT)
    client, _, penecho = bauen(ll.lern, JePenEcho(), kursende=tmp_path / "kursende.json", bewerter=bewerter2)

    def ohne_anordnung(*_: Any) -> None:
        raise AssertionError("_bereiche im Modell je Aufgabe")

    client.app._bereiche = ohne_anordnung
    try:
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen", headers={KOPF: KA})
        assert r.status_code == 200 and r.json()["abgeschlossen"] is True
        assert client.app._modellspur.wartend(f"{SID}:kurs") == 0
        (bericht,) = [a for a in penecho.aufrufe if a[0] == "bericht_zeigen"]
        assert bericht[1] == "board-kurs" and "Sie rechnen sicher." in bericht[3] and FOLGT not in bericht[3]
        assert bewerter2.daten == [] and SID not in _schluessel(penecho)
    finally:
        client.app.beenden()


def test_oeffnen_nach_gescheiterter_ki_fragt_erneut(tmp_path: Path) -> None:
    # Ohne gespeicherte KI-Rueckmeldung fragt ein spaeteres Oeffnen die Flaechen-KI erneut (im Hintergrund).
    ll = _beendet(2, tmp_path)
    bewerter = KursBewerter(None)
    client, penecho = _kurs_oeffnen(ll, tmp_path, bewerter)
    bruecke = client.app
    try:
        _bis(lambda: _kurs_ruhig(bruecke), 2)
        assert len(bewerter.daten) == 1 and FOLGT not in _berichtstexte(penecho)[-1]
        bewerter.antwort = KI_ANTWORT
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen", headers={KOPF: KA})
        assert r.status_code == 200
        _bis(lambda: _kurs_ruhig(bruecke), 2)
        assert len(bewerter.daten) == 2 and _rueckmeldungen(tmp_path)[SID]["quelle"] == "ki_flaeche"
        assert "Sie rechnen sicher." in _berichtstexte(penecho)[-1]
    finally:
        bruecke.beenden()

def test_weiter_am_kursende_laesst_kursrueckmeldung_vorn(tmp_path: Path) -> None:
    ll = _neu_lern(2)
    client, penecho = _kurs_oeffnen(ll, tmp_path)
    try:
        for i, e in enumerate(ll.eintraege, start=1):  # anderswo abgegeben: Learning meldet den Kurs als beendet
            e["status"], e["vorbewertung"] = "bestaetigt", _vor(vid=_vid(i), gelesen=str(i))
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/weiter", headers={KOPF: KA})
        assert r.status_code == 200, r.text
        neu = penecho.aufrufe[vorher:]
        assert [a[1] for a in neu if a[0] == "bericht_zeigen"] == ["board-kurs"]
        assert [a[1] for a in neu if a[0] == "bereich_zeigen"] == ["board-kurs"]
        assert "aufgabe_setzen" not in [a[0] for a in neu] and SID not in _schluessel(penecho)
        assert r.json()["flaeche"] is True
    finally:
        client.app.beenden()


# ---- Task 9: „Kurs abgeben“ sammelt und bewertet alle Aufgaben-Canvas ------------------------------------------
# Die Tests laufen in `with client:`: eine Ereignisschleife fuer alle Anfragen, wie im Betrieb (uvicorn). Das Ende
# jedes Schritts laeuft auf ihr unter `_ablauf`.

def _kurs_abgeben(client: TestClient, kopf: bool = True) -> Any:
    return client.post(f"/lernen/api/sitzungen/{SID}/kurs-abgeben", headers={KOPF: KA} if kopf else {})


def _kursabgabe(client: TestClient) -> Any:
    return _stand(client)["kursabgabe"]


def _kursabgabe_vorbei(client: TestClient) -> Any:
    """Wartet, bis der Lauf nicht mehr laeuft und keine Arbeit mehr in der Modellspur steht; dann sein Stand."""
    _bis(lambda: _kursabgabe(client)["zustand"] != "laeuft"
         and client.app._modellspur.wartend(f"{SID}:kursabgabe") == 0, 3)
    return _kursabgabe(client)


def _buchungen(ll: ListenLern, tool: str) -> list[dict[str, Any]]:
    return [kw["payload"] for kw in _aufrufe(ll.lern, tool)]


def _aid(ll: ListenLern, n: int) -> str:
    return ll.eintraege[n - 1]["attempt_id"]


def _zeigen(client: TestClient, nummer: int) -> None:
    r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": nummer}, headers={KOPF: KA})
    assert r.status_code == 200, r.text


def test_kurs_abgeben_bucht_alle_faelle(tmp_path: Path) -> None:
    # 1 bestaetigt, 2 mit Canvas (beim Oeffnen gezeigt, darauf gezeichnet), 3 nie geoeffnet.
    ll = _neu_lern(3)
    ll.eintraege[0]["status"], ll.eintraege[0]["vorbewertung"] = "bestaetigt", _vor(vid=_vid(1), gelesen="1")
    client, penecho = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    with client:
        try:
            assert bruecke._sitzungen[SID].aufgaben_boards == {2: "board-2"}
            vorher = len(penecho.aufrufe)
            r = _kurs_abgeben(client)
            assert r.status_code == 202, r.text
            assert r.json()["kursabgabe"] == {"zustand": "laeuft", "aktuell": 1, "gesamt": 3, "meldung": None}
            assert _kursabgabe_vorbei(client) == {"zustand": "fertig", "aktuell": 3, "gesamt": 3, "meldung": None}
            neu = penecho.aufrufe[vorher:]
            # Nur Aufgabe 2 wird aufgenommen und vorbewertet; 3 bekommt kein Canvas und wird leer gebucht.
            assert [a[1:] for a in neu if a[0] == "aufgabe_aufnehmen"] == [("board-2", EINZEL_RECHTECK, True)]
            assert "aufgabe_setzen" not in [a[0] for a in neu]
            assert _aufnahme(tmp_path, 2).read_bytes() == PNG
            (vorschau,) = _buchungen(ll, "learning_task_preview")
            assert vorschau["attempt_id"] == _aid(ll, 2) and "handschrift_png_base64" in vorschau
            assert _buchungen(ll, "learning_task_confirm") == [{"vorbewertung_id": _vid(2), "attempt_id": _aid(ll, 2)}]
            assert _buchungen(ll, "learning_task_answer") == [{"answer": {}, "attempt_id": _aid(ll, 3)}]
            assert all(e["status"] == "bestaetigt" for e in ll.eintraege)
            # Kursende: gemerkt, Kursbericht auf dem Kurs-Canvas.
            assert SID in json.loads((tmp_path / "kursende.json").read_text(encoding="utf-8")).values()
            assert [a[1] for a in neu if a[0] == "bericht_zeigen"] == ["board-kurs"]
            assert bruecke._sitzungen[SID].bericht_gezeigt is True
            assert SID not in _schluessel(penecho)
        finally:
            bruecke.beenden()


class HalteAufnahme(JePenEcho):
    """Haelt jede Aufnahme an, bis `frei` gesetzt ist (wie eine belegte Flaeche, nur ohne Fehler)."""

    def __init__(self) -> None:
        super().__init__()
        self.frei = threading.Event()

    def aufgabe_aufnehmen(self, session_id: str, rechteck: dict[str, int], *, ergebnis: bool,
                          warten_s: float = 10.0) -> bytes:
        assert self.frei.wait(5), "Aufnahme nie freigegeben"
        return super().aufgabe_aufnehmen(session_id, rechteck, ergebnis=ergebnis, warten_s=warten_s)


def test_kurs_abgeben_doppelt_ein_lauf(tmp_path: Path) -> None:
    ll = _neu_lern(2)
    penecho = HalteAufnahme()
    client, _ = _kurs_oeffnen(ll, tmp_path, penecho=penecho)
    bruecke = client.app
    with client:
        try:
            _zeigen(client, 2)
            assert _kurs_abgeben(client).status_code == 202
            _bis(lambda: bruecke._modellspur.zustand(f"{SID}:kursabgabe:1:1") == "laeuft", 2)
            r = _kurs_abgeben(client)
            assert r.status_code == 202, r.text
            assert r.json()["kursabgabe"]["zustand"] == "laeuft"
            assert bruecke._modellspur.wartend(f"{SID}:kursabgabe") == 1  # nichts zusaetzlich eingereiht
            penecho.frei.set()
            assert _kursabgabe_vorbei(client)["zustand"] == "fertig"
            assert [p["attempt_id"] for p in _buchungen(ll, "learning_task_confirm")] == [_aid(ll, 1), _aid(ll, 2)]
            assert len(_buchungen(ll, "learning_task_preview")) == 2
        finally:
            penecho.frei.set()
            bruecke.beenden()


def test_kurs_abgeben_ohne_flaeche_409(tmp_path: Path) -> None:
    ll = _neu_lern(2)
    client, _, _ = _oeffnen(ll, tmp_path)  # ohne Kopf X-Lernflaeche: keine eigene Flaeche
    try:
        r = _kurs_abgeben(client, kopf=False)
        assert r.status_code == 409 and r.json() == {"fehler": "KEINE_FLAECHE"}
        assert client.app._modellspur.wartend(f"{SID}:kursabgabe") == 0
        assert _kursabgabe(client) is None
        assert not {"learning_task_preview", "learning_task_confirm", "learning_task_answer"} & set(ll.lern.tools())
    finally:
        client.app.beenden()


def test_kurs_abgeben_alte_sitzung_409(tmp_path: Path) -> None:
    (tmp_path / "je-aufgabe.json").write_text(json.dumps(["andere-sitzung"]), encoding="utf-8")
    ll = ListenLern(3)
    client, penecho, _ = _oeffnen(ll, tmp_path, FakePenEcho())
    try:
        r = _kurs_abgeben(client)
        assert r.status_code == 409 and r.json() == {"fehler": "KURS_ABGEBEN_ALT"}
        assert client.app._modellspur.wartend(f"{SID}:kursabgabe") == 0
        assert _kursabgabe(client) is None
        assert "aufgabe_aufnehmen" not in penecho.namen() and "schnappschuss" not in penecho.namen()
        assert not {"learning_task_preview", "learning_task_confirm", "learning_task_answer"} & set(ll.lern.tools())
    finally:
        client.app.beenden()


def test_kurs_abgeben_fehler_und_fortsetzen(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    client, penecho = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    with client:
        try:
            _zeigen(client, 2)
            # Die Flaeche bleibt bei Aufgabe 2 belegt (auch nach dem Warten in `aufgabe_aufnehmen`).
            penecho.aufnahme_fehler = lambda rechteck: (
                PenEchoBeschaeftigt("CANVAS_BUSY") if penecho.aufrufe[-1][1] == "board-2" else None)
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client) == {
                "zustand": "fehler", "aktuell": 2, "gesamt": 3, "meldung": "FLAECHE_BESCHAEFTIGT"}
            assert [e["status"] for e in ll.eintraege] == ["bestaetigt", "offen", "offen"]  # Gebuchtes bleibt
            assert _buchungen(ll, "learning_task_answer") == []
            assert not (tmp_path / "kursende.json").exists()
            # Erneut abgeben: macht weiter, Aufgabe 1 wird nicht noch einmal gebucht.
            penecho.aufnahme_fehler = None
            r = _kurs_abgeben(client)
            assert r.status_code == 202 and r.json()["kursabgabe"]["zustand"] == "laeuft"
            assert _kursabgabe_vorbei(client) == {"zustand": "fertig", "aktuell": 3, "gesamt": 3, "meldung": None}
            assert [p["attempt_id"] for p in _buchungen(ll, "learning_task_confirm")] == [_aid(ll, 1), _aid(ll, 2)]
            assert [p["attempt_id"] for p in _buchungen(ll, "learning_task_preview")] == [_aid(ll, 1), _aid(ll, 2)]
            assert _buchungen(ll, "learning_task_answer") == [{"answer": {}, "attempt_id": _aid(ll, 3)}]
            assert SID in json.loads((tmp_path / "kursende.json").read_text(encoding="utf-8")).values()
        finally:
            bruecke.beenden()


# ---- Task 9, Fix 1 ---------------------------------------------------------------------------------------------

def test_kurs_abgeben_schritt_ende_unter_ablauf(tmp_path: Path) -> None:
    # Was nach der Buchung geteilten Zustand anfasst (Sperre, Kursende, Bericht), laeuft unter `_ablauf`, nie im
    # Faden der Modellspur ohne ihn.
    ll = _neu_lern(3)
    client, _ = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    protokoll: list[tuple[str, bool, str]] = []

    def mitschreiben(name: str) -> None:
        echt = getattr(bruecke, name)

        def aufruf(*a: Any, **kw: Any) -> Any:
            protokoll.append((name, bruecke._ablauf.locked(), threading.current_thread().name))
            return echt(*a, **kw)

        setattr(bruecke, name, aufruf)

    for name in ("_nach_buchung", "_frei", "_ende_setzen", "_bericht", "_kursbericht"):
        mitschreiben(name)
    with client:
        try:
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client)["zustand"] == "fertig"
            namen = {p[0] for p in protokoll}
            assert {"_nach_buchung", "_frei", "_ende_setzen", "_kursbericht"} <= namen
            assert [p[0] for p in protokoll].count("_nach_buchung") == 3
            assert all(gesperrt for _, gesperrt, _ in protokoll), protokoll
            assert all(faden != "spur-modell" for _, _, faden in protokoll), protokoll
        finally:
            bruecke.beenden()


def test_kurs_abgeben_wartet_auf_ablauf(tmp_path: Path) -> None:
    # Haelt eine Route `_ablauf`, wartet das Ende des Schritts (die Buchung selbst ist schon geschehen).
    ll = _neu_lern(1)
    client, _ = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    halt, drin = threading.Event(), threading.Event()
    echt = bruecke._weiter

    def langsam(*a: Any, **kw: Any) -> Any:
        drin.set()
        assert halt.wait(5)
        return echt(*a, **kw)

    bruecke._weiter = langsam
    with client:
        try:
            faden = threading.Thread(target=lambda: client.post(f"/lernen/api/sitzungen/{SID}/weiter",
                                                                  headers={KOPF: KA}))
            faden.start()
            assert drin.wait(2)
            assert _kurs_abgeben(client).status_code == 202
            _bis(lambda: _buchungen(ll, "learning_task_confirm") != [], 2)
            assert not (tmp_path / "kursende.json").exists()  # das Ende wartet auf `_ablauf`
            assert _kursabgabe(client)["zustand"] == "laeuft"
            halt.set()
            faden.join(5)
            assert _kursabgabe_vorbei(client)["zustand"] == "fertig"
            assert SID in json.loads((tmp_path / "kursende.json").read_text(encoding="utf-8")).values()
        finally:
            halt.set()
            bruecke.beenden()


def _unlesbar_bei(ll: ListenLern, nummern: set[int]) -> None:
    """Die Vorbewertung dieser Aufgaben ist nicht lesbar (etwa ein leeres, selbst angelegtes Canvas)."""
    def vorbewerten(kw: dict[str, Any]) -> dict[str, Any]:
        n = ll._nummer(kw)
        if n not in nummern:
            return ll._vorbewerten(kw)
        vor = _vor(lesbar=False, vid=_vid(n), gelesen=None)
        ll.eintraege[n - 1]["vorbewertung"] = vor
        return {"session": ll.turn(), "vorbewertung": vor}

    ll.lern.antworten["learning_task_preview"] = vorbewerten


UNLESBAR_2 = ("Aufgabe 2 konnte nicht gelesen werden. Bitte schreiben Sie deutlicher oder tippen Sie Ihre Antwort über "
              "„Falsch gelesen“ ein. Wenn Sie sie nicht bearbeiten möchten, drücken Sie noch einmal „Kurs abgeben“ – "
              "dann zählt sie als nicht bearbeitet.")


def test_kurs_abgeben_unlesbar_gleiche_aufnahme_zaehlt_nicht_bearbeitet(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    _unlesbar_bei(ll, {2})
    client, penecho = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    with client:
        try:
            _unlesbar_lauf_in(ll, client, penecho, tmp_path)
            # Zweiter Lauf, dieselbe Aufnahme: nicht bearbeitet, ohne neue Vorbewertung.
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client) == {"zustand": "fertig", "aktuell": 3, "gesamt": 3, "meldung": None}
            assert len(_buchungen(ll, "learning_task_preview")) == 3
            assert _buchungen(ll, "learning_task_answer") == [{"answer": {}, "attempt_id": _aid(ll, 2)}]
            assert SID in json.loads((tmp_path / "kursende.json").read_text(encoding="utf-8")).values()
        finally:
            bruecke.beenden()


def test_kurs_abgeben_unlesbar_geaenderte_aufnahme_wird_bewertet(tmp_path: Path) -> None:
    ll = _neu_lern(3)
    _unlesbar_bei(ll, {2})
    client, penecho = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    with client:
        try:
            _unlesbar_lauf_in(ll, client, penecho, tmp_path)
            penecho.bild = PNG + b"deutlicher"
            _unlesbar_bei(ll, set())
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client)["zustand"] == "fertig"
            assert [p["attempt_id"] for p in _buchungen(ll, "learning_task_preview")][-1] == _aid(ll, 2)
            assert _buchungen(ll, "learning_task_confirm")[-1] == {"vorbewertung_id": _vid(2),
                                                                   "attempt_id": _aid(ll, 2)}
            assert _buchungen(ll, "learning_task_answer") == []
            assert _aufnahme(tmp_path, 2).read_bytes() == PNG + b"deutlicher"
        finally:
            bruecke.beenden()


def _unlesbar_lauf_in(ll: ListenLern, client: TestClient, penecho: FakePenEcho, tmp_path: Path) -> None:
    """Erster Lauf: 3 Aufgaben mit Canvas, Aufgabe 2 nicht lesbar; er endet mit `fehler` und dem festen Text."""
    _zeigen(client, 2)
    _zeigen(client, 3)
    assert _kurs_abgeben(client).status_code == 202
    assert _kursabgabe_vorbei(client) == {"zustand": "fehler", "aktuell": 3, "gesamt": 3, "meldung": UNLESBAR_2}
    _bis(lambda: _ruhig(client.app), 2)
    assert [e["status"] for e in ll.eintraege] == ["bestaetigt", "offen", "bestaetigt"]
    assert [p["attempt_id"] for p in _buchungen(ll, "learning_task_confirm")] == [_aid(ll, 1), _aid(ll, 3)]
    assert _buchungen(ll, "learning_task_answer") == []
    # Die Karte der Vorbewertung kommt auf das Canvas der Aufgabe (wie bei „Abgeben“).
    (karte,) = [k for k in _karten(penecho) if k[3] == 2]
    assert karte[1] == "board-2" and karte[5] == "Nicht lesbar."
    assert not (tmp_path / "kursende.json").exists()


def test_kurs_abgeben_getippte_korrektur_wird_bestaetigt(tmp_path: Path) -> None:
    # Aufgabe 1: getippte, lesbare Vorbewertung -> so bestaetigt, ohne Aufnahme. Aufgabe 2: Vorbewertung aus
    # Handschrift -> neu aufgenommen und bewertet.
    ll = _neu_lern(2)
    ll.eintraege[0]["vorbewertung"] = _vor(vid=_vid(11), gelesen="1", quelle="getippt")
    ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(12), gelesen="2")
    client, penecho = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    revision = ll.turn()["session_revision"]
    with client:
        try:
            _zeigen(client, 2)
            vorher = len(penecho.aufrufe)
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client)["zustand"] == "fertig"
            assert [a[1] for a in penecho.aufrufe[vorher:] if a[0] == "aufgabe_aufnehmen"] == ["board-2"]
            assert [p["attempt_id"] for p in _buchungen(ll, "learning_task_preview")] == [_aid(ll, 2)]
            assert _buchungen(ll, "learning_task_confirm") == [
                {"vorbewertung_id": _vid(11), "attempt_id": _aid(ll, 1)},
                {"vorbewertung_id": _vid(2), "attempt_id": _aid(ll, 2)}]
            (kw,) = [kw for kw in _aufrufe(ll.lern, "learning_task_confirm")
                     if kw["payload"]["attempt_id"] == _aid(ll, 1)]
            assert kw["expected_revision"] == revision
        finally:
            bruecke.beenden()


def test_kurs_abgeben_schon_gebucht_zaehlt_fertig(tmp_path: Path) -> None:
    # Learning meldet bei der eigenen Buchung „schon abgegeben“ (anderswo gebucht): zaehlt als fertig, weiter.
    ll = _neu_lern(3)
    client, penecho = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    echt_bestaetigen, echt_beantworten = ll._bestaetigen, ll._beantworten

    def bestaetigen(kw: dict[str, Any]) -> dict[str, Any]:
        if ll._nummer(kw) == 1:
            ll.eintraege[0]["status"] = "bestaetigt"
            raise LearningAbgelehnt("vorbewertung not current", "session_state_conflict")
        return echt_bestaetigen(kw)

    def beantworten(kw: dict[str, Any]) -> dict[str, Any]:
        if ll._nummer(kw) == 3:
            ll.eintraege[2]["status"] = "bestaetigt"
            raise LearningAbgelehnt("adaptive task was already answered", "session_state_conflict")
        return echt_beantworten(kw)

    ll.lern.antworten["learning_task_confirm"] = bestaetigen
    ll.lern.antworten["learning_task_answer"] = beantworten
    with client:
        try:
            _zeigen(client, 2)
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client) == {"zustand": "fertig", "aktuell": 3, "gesamt": 3, "meldung": None}
            assert [p["attempt_id"] for p in _buchungen(ll, "learning_task_confirm")] == [_aid(ll, 1), _aid(ll, 2)]
            assert [p["attempt_id"] for p in _buchungen(ll, "learning_task_answer")] == [_aid(ll, 3)]
            assert SID in json.loads((tmp_path / "kursende.json").read_text(encoding="utf-8")).values()
            assert [a[1] for a in penecho.aufrufe if a[0] == "bericht_zeigen"] == ["board-kurs"]
        finally:
            bruecke.beenden()


def test_kurs_abgeben_unlesbar_erst_nach_mitteilung_nicht_bearbeitet(tmp_path: Path) -> None:
    # Aufgabe 2 unlesbar, danach scheitert Aufgabe 3 (Flaeche belegt): der Lauf endet ohne den Unlesbar-Text. Der
    # naechste Druck bewertet 2 erneut (kein `{}`); erst nach einem Lauf, der den Text zeigte, zaehlt dieselbe
    # Aufnahme als nicht bearbeitet.
    ll = _neu_lern(3)
    _unlesbar_bei(ll, {2})
    client, penecho = _kurs_oeffnen(ll, tmp_path)
    bruecke = client.app
    with client:
        try:
            _zeigen(client, 2)
            _zeigen(client, 3)
            penecho.aufnahme_fehler = lambda rechteck: (
                PenEchoBeschaeftigt("CANVAS_BUSY") if penecho.aufrufe[-1][1] == "board-3" else None)
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client) == {
                "zustand": "fehler", "aktuell": 3, "gesamt": 3, "meldung": "FLAECHE_BESCHAEFTIGT"}
            vorschauen_2 = lambda: [p for p in _buchungen(ll, "learning_task_preview")  # noqa: E731
                                    if p["attempt_id"] == _aid(ll, 2)]
            assert len(vorschauen_2()) == 1
            # Zweiter Druck, dieselbe Aufnahme: 2 wird erneut bewertet (der Text wurde nie gezeigt).
            penecho.aufnahme_fehler = None
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client) == {"zustand": "fehler", "aktuell": 3, "gesamt": 3, "meldung": UNLESBAR_2}
            assert len(vorschauen_2()) == 2 and _buchungen(ll, "learning_task_answer") == []
            assert [e["status"] for e in ll.eintraege] == ["bestaetigt", "offen", "bestaetigt"]
            # Dritter Druck nach dem gezeigten Text: dieselbe Aufnahme zaehlt als nicht bearbeitet.
            _bis(lambda: _ruhig(bruecke), 2)
            assert _kurs_abgeben(client).status_code == 202
            assert _kursabgabe_vorbei(client)["zustand"] == "fertig"
            assert len(vorschauen_2()) == 2
            assert _buchungen(ll, "learning_task_answer") == [{"answer": {}, "attempt_id": _aid(ll, 2)}]
        finally:
            bruecke.beenden()
