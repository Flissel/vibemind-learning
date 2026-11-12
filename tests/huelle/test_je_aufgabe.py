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
from spaces.learning.huelle.penecho_mcp import PenEchoBeschaeftigt, PenEchoNichtErreichbar, aufgaben_titel
from spaces.learning.huelle.zusammenfassung import Kursrueckmeldung

from .test_bruecke import (
    ACTOR, GID, KA, KOPF, KURS, PA, PNG, SID, SID_GA, FakePenEcho, Laeufe, ListenLern, TabMcp, _bis,
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
        neu = penecho.aufrufe[vorher:]
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
        assert penecho.aufrufe[vorher:] == [("sitzung", f"{SID}#2", "Aufgabe 2: Frage 2"),
                                            ("bereich_zeigen", "board-2", EINZEL_RECHTECK),
                                            ("dokument_merken", "board-2")]
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
        neu = [a[0] for a in penecho.aufrufe[vorher:] if a[0] != "sitzung"]
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
    """Bewerter mit Kursrueckmeldung (wie KiBewerter); `antwort` ist die Antwort der Flaechen-KI oder None."""

    aktiv = True

    def __init__(self, antwort: dict[str, Any] | None) -> None:
        self.antwort = antwort
        self.daten: list[dict[str, Any]] = []

    def bewerten(self, bild_png: bytes, daten: dict[str, Any]) -> None:
        return None

    def kursrueckmeldung(self, daten: dict[str, Any]) -> dict[str, Any] | None:
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


def test_kursbericht_ki_und_schliessen(tmp_path: Path) -> None:
    ll = _neu_lern(2)
    bewerter = KursBewerter(KI_ANTWORT)
    client, penecho = _kurs_oeffnen(ll, tmp_path, bewerter)
    try:
        _abgeben_passt(client, 1)
        vorher = len(penecho.aufrufe)
        _abgeben_passt(client, 2)
        neu = penecho.aufrufe[vorher:]
        assert ("sitzung", f"{SID}#kurs", "Kursrückmeldung", KA) in neu
        (bericht,) = [a for a in neu if a[0] == "bericht_zeigen"]
        assert bericht[1:3] == ("board-kurs", SID) and bericht[4] == (1, 2)
        assert penecho.bericht_bilder[-1] == {1: PNG, 2: PNG} and penecho.bericht_rechtecke[-1] is None
        text = bericht[3]
        assert text.startswith("# Bericht: ") and "## Kursrückmeldung" in text
        assert "**Stärken:** Sie rechnen sicher." in text and "**Nächste Schritte:** Üben Sie Einheiten." in text
        assert ("bereich_zeigen", "board-kurs", EINZEL_RECHTECK) in neu
        assert ("dokument_merken", "board-kurs") in neu
        assert [a[1:] for a in neu if a[0] == "dokument_schliessen"] == [(f"{SID}#1", KA), (f"{SID}#2", KA)]
        # Nach dem Bericht holt nichts mehr ein Aufgaben-Canvas nach vorn.
        i = max(k for k, a in enumerate(neu) if a[0] == "bericht_zeigen")
        assert not [a for a in neu[i:] if a[0] in ("bereich_zeigen", "aufgabe_setzen")]
        assert _rueckmeldungen(tmp_path)[SID] == {
            "staerken": "Sie rechnen sicher.", "luecken": "Einheiten fehlen.",
            "naechste_schritte": "Üben Sie Einheiten.", "quelle": "ki_flaeche"}
        (daten,) = bewerter.daten
        assert set(daten) == {"kurs", "aufgaben", "begriffe"}
        assert [a["nummer"] for a in daten["aufgaben"]] == [1, 2] and "musterloesung" not in repr(daten)
        assert "schnappschuss" not in penecho.namen() and "ergebnis_schnappschuss" not in penecho.namen()
        assert SID not in _schluessel(penecho) and client.app._sitzungen[SID].board is None
        assert client.app._sitzungen[SID].bericht_gezeigt is True
    finally:
        client.app.beenden()


@pytest.mark.parametrize("bewerter", [KursBewerter(None), None], ids=["endpunkt-none", "ohne-bewerter"])
def test_kursbericht_ohne_endpunkt_und_ohne_schliessen(tmp_path: Path, bewerter: Any) -> None:
    ll = _neu_lern(2)
    penecho = JePenEcho()
    penecho.schliessen_ergebnis = False
    client, _ = _kurs_oeffnen(ll, tmp_path, bewerter, penecho)
    try:
        _abgeben_passt(client, 1)
        _abgeben_passt(client, 2)
        eintrag = _rueckmeldungen(tmp_path)[SID]
        # BERICHT (Learning): Aufgabe 1 mit 1.0 richtig, Aufgabe 2 mit 0.7 nur teilweise.
        assert eintrag == {"staerken": "1 von 2 Aufgaben richtig.", "luecken": "Offen: Aufgabe 2.",
                           "naechste_schritte": "Wiederholen Sie die offenen Aufgaben.", "quelle": "zahlen"}
        assert SID in json.loads((tmp_path / "kursende.json").read_text(encoding="utf-8")).values()
        assert penecho.namen().count("dokument_schliessen") == 2
        (bericht,) = [a for a in penecho.aufrufe if a[0] == "bericht_zeigen"]
        assert "**Stärken:** 1 von 2 Aufgaben richtig." in bericht[3]
    finally:
        client.app.beenden()


def test_zusammenfassung_je_aufgabe_aus_aufnahmen_auch_ueber_zwanzig(tmp_path: Path) -> None:
    ll = _neu_lern(45)
    client, penecho, _ = _oeffnen(ll, tmp_path)
    bruecke = client.app
    try:
        bruecke._aufnahme_merken(SID, 1, PNG)
        bruecke._aufnahme_merken(SID, 30, PNG)
        bruecke._kursrueckmeldung_merken(SID, Kursrueckmeldung("S", "L", "N", "zahlen"))
        assert _rueckmeldungen(tmp_path)[SID]["staerken"] == "S"
        vorher = len(penecho.aufrufe)
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert penecho.aufrufe[vorher:] == []  # keine Aufnahme, kein Board
        z = bruecke._zusammenfassung_bauen(bruecke._sitzungen[SID], bilder=True)
        assert [a.bild for a in z.aufgaben][:2] == [PNG, None] and z.aufgaben[29].bild == PNG
        assert z.rueckmeldung is not None and (z.rueckmeldung.staerken, z.rueckmeldung.quelle) == ("S", "zahlen")
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
        assert [a[1] for a in penecho.aufrufe if a[0] == "bericht_zeigen"] == ["board-kurs"]
        assert SID not in _schluessel(penecho) and len(bewerter.daten) == 1
        assert _rueckmeldungen(tmp_path)[SID]["quelle"] == "ki_flaeche"
        # Erneutes Oeffnen: dieselbe Kursrueckmeldung nach vorn, kein zweiter Bericht, keine zweite KI-Anfrage.
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen", headers={KOPF: KA})
        assert r.status_code == 200 and r.json()["abgeschlossen"] is True
        neu = penecho.aufrufe[vorher:]
        assert ("bereich_zeigen", "board-kurs", EINZEL_RECHTECK) in neu and "bericht_zeigen" not in [a[0] for a in neu]
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
        (bericht,) = [a for a in penecho.aufrufe if a[0] == "bericht_zeigen"]
        assert bericht[1] == "board-kurs" and "Sie rechnen sicher." in bericht[3]
        assert bewerter2.daten == [] and SID not in _schluessel(penecho)
    finally:
        client.app.beenden()


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
