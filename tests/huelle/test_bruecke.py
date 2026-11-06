"""Bruecke der Lern-Huelle: Routen unter /lernen/api/ mit Fakes fuer Learning und PenEcho."""

from __future__ import annotations

import base64
import io
import json
import re
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from spaces.learning.huelle.bruecke import (
    BEWERTUNG_WEG, FLAECHE_WEG, KEIN_KURS, KEINE_FLAECHENAUFGABE, KEINE_KORREKTUR, NICHT_ANGENOMMEN,
    NICHT_BESTAETIGT, NICHT_ERREICHBAR, NUR_UEBEN, SCHON_ABGEGEBEN, WIRD_GEPRUEFT, Bruecke, urteil_aus,
)
from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.lern_mcp import LearningAbgelehnt, LearningWeg, LernMcp, idempotenz_schluessel
from spaces.learning.huelle.penecho_mcp import (
    URTEILE, Aufgabenbild, PenEchoNichtErreichbar, PenEchoSitzungUnbekannt, bereich,
)
from spaces.learning.huelle.sperre import Sperre
from spaces.learning.huelle.tafel import aufgabentext

KURS = "11111111-1111-4111-8111-111111111111"
FREMD = "22222222-2222-4222-8222-222222222222"
SID = "33333333-3333-4333-8333-333333333333"
TASK_1 = "44444444-4444-4444-8444-444444444441"
TASK_2 = "44444444-4444-4444-8444-444444444442"
ACTOR = "lh-1-test"
SCHLUESSEL = "k" * 40
API = "http://learning-api.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16

E = einstellung_lesen({
    "LEARNING_HUELLE_BASIS": "https://lernen.example.test",
    "LEARNING_HUELLE_FLAECHE_BASIS": "https://lernen.example.test:8443",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNING_HUELLE_API_URL": API,
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
    "LEARNING_HUELLE_ACTOR": ACTOR,
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": KURS, "titel": "Testkurs"}]),
})


# Vorgabe `exam`: nur Pruefungsaufgaben schliessen die Sperre, die Sperr-Tests brauchen also eine.
def turn(task_id: str | None = TASK_1, *, completed: int = 0, revision: int = 1, state: str = "active",
         typ: str = "single_choice", feedback: dict[str, Any] | None = None, course_id: str = KURS,
         mode: str | None = "exam") -> dict[str, Any]:
    task = None if task_id is None else {
        "id": task_id, "type": typ, "prompt": f"Frage {task_id[-1]}", "options": [], "difficulty": 0.5,
        "representation": "text", "source_refs": [], "presentation": {},
    }
    werte = {
        "session_id": SID, "course_id": course_id, "session_revision": revision, "state": state,
        "mode": mode, "task": task, "progress": {"completed": completed, "total": 5},
        "feedback": feedback,
    }
    if mode is None:
        del werte["mode"]
    return werte


class FakeLern:
    def __init__(self, antworten: dict[str, Any]) -> None:
        self.antworten = antworten
        self.aufrufe: list[tuple[str, dict[str, Any]]] = []

    def rufe(self, tool: str, event_type: str, **kw: Any) -> dict[str, Any]:
        self.aufrufe.append((tool, kw))
        wert = self.antworten[tool]
        if isinstance(wert, list):
            wert = wert.pop(0) if len(wert) > 1 else wert[0]
        if isinstance(wert, Exception):
            raise wert
        return wert(kw) if callable(wert) else wert

    def tools(self) -> list[str]:
        return [t for t, _ in self.aufrufe]


class FakePenEcho:
    def __init__(self, *, fehler: Exception | None = None, bild: bytes = PNG) -> None:
        self.fehler = fehler
        self.bild = bild
        self.aufrufe: list[tuple[Any, ...]] = []
        self.unbekannt_einmal = False
        # (aufgabe_id, nummer, mit_ergebnisfeld) je aufgabe_setzen; das alte Tupel bleibt fuer die alten Tests.
        self.ergebnisfelder: list[tuple[str, int, bool]] = []
        self.arbeitsfelder: list[tuple[str, int, bool]] = []
        self.bilder: list[list[Aufgabenbild]] = []
        self.merken_fehler: Exception | None = None

    def _pruefen(self, name: str) -> None:
        if self.unbekannt_einmal:
            self.unbekannt_einmal = False
            raise PenEchoSitzungUnbekannt("session_expired")
        if self.fehler is not None:
            raise self.fehler

    def sitzung(self, schluessel: str, titel: str) -> str:
        self.aufrufe.append(("sitzung", schluessel, titel))
        return "pe-1"

    def vergessen(self, schluessel: str) -> None:
        self.aufrufe.append(("vergessen", schluessel))

    def aufgabe_setzen(self, session_id: str, aufgabe_id: str, text: str, nummer: int, *,
                       mit_ergebnisfeld: bool = False, mit_arbeitsfeld: bool = False,
                       bilder: Any = ()) -> None:
        self.aufrufe.append(("aufgabe_setzen", session_id, aufgabe_id, text, nummer))
        self.bilder.append(list(bilder))
        self.ergebnisfelder.append((aufgabe_id, nummer, mit_ergebnisfeld))
        self.arbeitsfelder.append((aufgabe_id, nummer, mit_arbeitsfeld))
        bereich(nummer)  # wie PenEchoMcp: ausserhalb der 20 Bereiche ValueError
        self._pruefen("aufgabe_setzen")

    def ergebnis_schnappschuss(self, session_id: str, nummer: int) -> bytes:
        self.aufrufe.append(("ergebnis_schnappschuss", session_id, nummer))
        self._pruefen("ergebnis_schnappschuss")
        return self.bild

    def rueckmeldung_zeigen(self, session_id: str, aufgabe_id: str, nummer: int, *,
                            gelesen: str | None, urteil: str, text: str) -> None:
        self.aufrufe.append(("rueckmeldung_zeigen", session_id, aufgabe_id, nummer, gelesen, urteil, text))
        bereich(nummer)
        if urteil not in URTEILE:  # wie PenEchoMcp
            raise ValueError("unbekanntes Urteil")
        self._pruefen("rueckmeldung_zeigen")

    def dokument_merken(self, session_id: str) -> bool:
        self.aufrufe.append(("dokument_merken", session_id))
        if self.merken_fehler is not None:
            raise self.merken_fehler
        return True

    def rueckmeldung_setzen(self, session_id: str, aufgabe_id: str, nummer: int, titel: str, text: str) -> None:
        self.aufrufe.append(("rueckmeldung_setzen", session_id, aufgabe_id, nummer, titel, text))
        bereich(nummer)
        self._pruefen("rueckmeldung_setzen")

    def schnappschuss(self, session_id: str, nummer: int) -> bytes:
        self.aufrufe.append(("schnappschuss", session_id, nummer))
        self._pruefen("schnappschuss")
        return self.bild

    def bericht_zeigen(self, session_id: str, sid: str, text: str, nummern: tuple[int, ...] = ()) -> None:
        self.aufrufe.append(("bericht_zeigen", session_id, sid, text, tuple(nummern)))
        self._pruefen("bericht_zeigen")

    def namen(self) -> list[str]:
        return [a[0] for a in self.aufrufe]


def _ueberblick(session: dict[str, Any] | None, revision: int | None = 3) -> dict[str, Any]:
    return {"course_id": KURS, "course_revision": revision,
            "begriffe": [{"concept_key": "k1", "titel": "Begriff", "beherrschung": None}], "session": session}


def _api_leer(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


def bauen(lern: FakeLern, penecho: FakePenEcho | None = None, *, api=_api_leer,
          sperre: Sperre | None = None, einstellung=E, kursende=None) -> tuple[TestClient, Sperre, FakePenEcho]:
    sperre = sperre or Sperre()
    penecho = penecho or FakePenEcho()
    http = httpx.Client(transport=httpx.MockTransport(api))
    bruecke = Bruecke(einstellung, lern, penecho, sperre, http, kursende)  # type: ignore[arg-type]
    return TestClient(bruecke), sperre, penecho


def _offen(lern: FakeLern, penecho: FakePenEcho | None = None, **kw: Any):
    client, sperre, penecho = bauen(lern, penecho, **kw)
    r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    assert r.status_code == 200, r.text
    return client, sperre, penecho, r.json()


# ---- oeffnen ------------------------------------------------------------------

def test_kurse_liste() -> None:
    client, _, _ = bauen(FakeLern({}))
    assert client.get("/lernen/api/kurse").json() == [{"course_id": KURS, "titel": "Testkurs"}]


def test_oeffnen_setzt_fort_statt_neu() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(turn()), "learning_task_next": {"session": turn()}})
    _, _, penecho, body = _offen(lern)
    assert "learning_session_start" not in lern.tools()
    assert lern.tools() == ["learning_course_overview", "learning_task_next"]
    assert lern.aufrufe[1][1]["session_id"] == SID
    assert body["kurs"] == {"course_id": KURS, "titel": "Testkurs"}
    assert body["begriffe"][0]["concept_key"] == "k1"
    assert body["turn"]["task"]["id"] == TASK_1
    assert body["nummer"] == 1 and body["flaeche"] is True
    assert ("aufgabe_setzen", "pe-1", TASK_1, "Frage 1", 1) in penecho.aufrufe
    sitzungen = [a for a in penecho.aufrufe if a[0] == "sitzung"]
    assert sitzungen and all(a[1] == SID for a in sitzungen)
    assert re.fullmatch(r"Testkurs · \d{2}\.\d{2}\.\d{4}", sitzungen[0][2])


def test_oeffnen_startet_ohne_sitzung() -> None:
    lern = FakeLern({
        "learning_course_overview": _ueberblick(None),
        "learning_session_start": {"session": turn()},
        "learning_task_next": {"session": turn()},
    })
    _offen(lern)
    assert lern.tools() == ["learning_course_overview", "learning_session_start", "learning_task_next"]
    start = lern.aufrufe[1][1]
    assert start["course_id"] == KURS and start["schreibend"] is True
    assert start["payload"] == {"course_revision": 3, "mode": "training", "blueprint": {"max_attempts": 20}}


def test_oeffnen_ohne_revision_409_kein_kurs() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(None, revision=None)})
    client, _, _ = bauen(lern)
    r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    assert r.status_code == 409
    assert r.json() == {"meldung": KEIN_KURS}
    assert lern.tools() == ["learning_course_overview"]


def test_oeffnen_sperrt_bei_offener_aufgabe() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(turn()), "learning_task_next": {"session": turn()}})
    sperre = Sperre()
    sperre.frei()
    _, sperre, _, _ = _offen(lern, sperre=sperre)
    assert sperre.gesperrt() is True


def test_oeffnen_uebungsaufgabe_laesst_ki_frei() -> None:
    uebung = turn(mode="training")
    lern = FakeLern({"learning_course_overview": _ueberblick(uebung), "learning_task_next": {"session": uebung}})
    _, sperre, penecho, body = _offen(lern)
    assert sperre.gesperrt() is False
    assert body["turn"]["task"]["id"] == TASK_1
    assert ("aufgabe_setzen", "pe-1", TASK_1, "Frage 1", 1) in penecho.aufrufe


def test_oeffnen_uebung_loest_eine_alte_sperre() -> None:
    uebung = turn(mode="training")
    lern = FakeLern({"learning_course_overview": _ueberblick(uebung), "learning_task_next": {"session": uebung}})
    sperre = Sperre()
    sperre.offen(TASK_2)
    _, sperre, _, _ = _offen(lern, sperre=sperre)
    assert sperre.gesperrt() is False


def test_oeffnen_ohne_modus_bleibt_gesperrt() -> None:
    unklar = turn(mode=None)
    lern = FakeLern({"learning_course_overview": _ueberblick(unklar), "learning_task_next": {"session": unklar}})
    sperre = Sperre()
    sperre.frei()
    _, sperre, _, _ = _offen(lern, sperre=sperre)
    assert sperre.gesperrt() is True


def test_oeffnen_flaeche_weg_wird_geschluckt_sperre_trotzdem_zu() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(turn()), "learning_task_next": {"session": turn()}})
    sperre = Sperre()
    sperre.frei()
    _, sperre, _, body = _offen(lern, FakePenEcho(fehler=PenEchoNichtErreichbar("x")), sperre=sperre)
    assert body["flaeche"] is False and body["turn"]["task"]["id"] == TASK_1
    assert sperre.gesperrt() is True


def test_oeffnen_erneuert_unbekannte_penecho_sitzung_einmal() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(turn()), "learning_task_next": {"session": turn()}})
    penecho = FakePenEcho()
    penecho.unbekannt_einmal = True
    _, _, penecho, body = _offen(lern, penecho)
    assert body["flaeche"] is True
    assert penecho.namen() == ["sitzung", "aufgabe_setzen", "vergessen", "sitzung", "aufgabe_setzen"]


def test_unbekannter_kurs_404() -> None:
    lern = FakeLern({})
    client, _, _ = bauen(lern)
    assert client.post(f"/lernen/api/kurse/{FREMD}/oeffnen").status_code == 404
    assert client.post("/lernen/api/kurse/kein-uuid/oeffnen").status_code == 404
    assert client.get(f"/lernen/api/kurse/{FREMD}/bewertung").status_code == 404
    assert client.get(f"/lernen/api/notizen?course_id={FREMD}").status_code == 404
    assert client.get("/lernen/api/notizen").status_code == 404
    assert lern.aufrufe == []


def test_sitzung_eines_fremden_kurses_404() -> None:
    lern = FakeLern({"learning_task_next": {"session": turn(course_id=FREMD)}})
    client, _, _ = bauen(lern)
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 404
    assert "learning_task_answer" not in lern.tools()
    lern2 = FakeLern({"learning_task_next": LearningAbgelehnt("session belongs to another learner", "session_access_denied")})
    client2, _, _ = bauen(lern2)
    assert client2.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 404
    assert client2.post("/lernen/api/sitzungen/kein-uuid/weiter").status_code == 404


# ---- antwort / weiter ---------------------------------------------------------

def _offene_sitzung(antwort: Any, **kw: Any):
    lern = FakeLern({
        "learning_course_overview": _ueberblick(turn()),
        "learning_task_next": [{"session": turn()}],
        "learning_task_answer": antwort,
    })
    client, sperre, penecho, _ = _offen(lern, **kw)
    return lern, client, sperre, penecho


def test_antwort_gibt_frei_und_setzt_rueckmeldung() -> None:
    naechster = turn(TASK_2, completed=1, revision=2, feedback={"state": "incorrect", "score": 0.0,
                     "rationale_codes": [], "source_refs": [],
                     "criteria": [{"criterion_id": "c1", "description": "Begriff", "score": 0.5,
                                   "feedback": "Der Begriff fehlt."}]})
    lern, client, sperre, penecho = _offene_sitzung({"session": naechster})
    assert sperre.gesperrt() is True
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sperre.gesperrt() is False
    tool, kw = [a for a in lern.aufrufe if a[0] == "learning_task_answer"][-1]
    assert lern.aufrufe[-1][0] == "learning_course_overview"  # Begriffe nach der Abgabe
    assert kw["expected_revision"] == 1 and kw["payload"] == {"answer": {"choice_id": "a"}} and kw["schreibend"]
    karte = [a for a in penecho.aufrufe if a[0] == "rueckmeldung_setzen"]
    assert len(karte) == 1
    _, _, aufgabe_id, nummer, titel, text = karte[0]
    assert aufgabe_id == TASK_1 and nummer == 1 and titel == "Rückmeldung zu Aufgabe 1"
    assert "Noch nicht richtig." in text and "Begriff: Der Begriff fehlt." in text
    assert body["rueckmeldung"] == text and body["flaeche"] is True
    assert body["turn"]["task"]["id"] == TASK_2 and body["nummer"] == 2
    # Die naechste Aufgabe steht noch nicht auf der Flaeche.
    assert not any(a[0] == "aufgabe_setzen" and a[2] == TASK_2 for a in penecho.aufrufe)


def test_antwort_liefert_aktuelle_begriffe_und_keine_toten_verweise() -> None:
    # Die Seite zeigt keine Quellenbelege; der Text verweist auf die Erklaerung auf der Flaeche,
    # und die Balken der Begriffe kommen nach jeder Abgabe neu mit.
    naechster = turn(TASK_2, completed=1, revision=2, feedback={"state": "incorrect", "score": 0.0,
                     "rationale_codes": [], "source_refs": []})
    lern, client, _, _ = _offene_sitzung({"session": naechster})
    nachher = _ueberblick(naechster)
    nachher["begriffe"] = [{"concept_key": "k1", "titel": "Begriff", "beherrschung": 0.6}]
    lern.antworten["learning_course_overview"] = nachher
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    body = r.json()
    assert "Quellenbelege" not in body["rueckmeldung"]
    assert body["rueckmeldung"].startswith("Noch nicht richtig.") and "Fläche" in body["rueckmeldung"]
    assert body["begriffe"] == [{"concept_key": "k1", "titel": "Begriff", "beherrschung": 0.6}]


def test_kriterium_mit_satzende_ohne_doppelpunkt_salat() -> None:
    naechster = turn(TASK_2, completed=1, revision=2, feedback={"state": "incorrect", "score": 0.0,
                     "rationale_codes": [], "source_refs": [],
                     "criteria": [{"criterion_id": "c1", "description": "Benennt den Fehler.", "score": 0.0,
                                   "feedback": "Der Fehler fehlt (vgl. S5, S6)."}]})
    _, client, _, _ = _offene_sitzung({"session": naechster})
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert "Benennt den Fehler: Der Fehler fehlt." in r.json()["rueckmeldung"]


def test_antwort_ohne_ueberblick_bleibt_gueltig() -> None:
    naechster = turn(TASK_2, completed=1, revision=2, feedback={"state": "correct", "score": 1.0,
                     "rationale_codes": [], "source_refs": []})
    lern, client, _, _ = _offene_sitzung({"session": naechster})
    lern.antworten["learning_course_overview"] = LearningWeg("x")
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200 and r.json()["begriffe"] is None


def test_antwort_rueckmeldung_fehler_wird_geschluckt() -> None:
    naechster = turn(TASK_2, completed=1, revision=2, feedback={"state": "correct", "score": 1.0,
                     "rationale_codes": [], "source_refs": []})
    lern, client, sperre, penecho = _offene_sitzung({"session": naechster})
    penecho.fehler = PenEchoNichtErreichbar("weg")
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200
    assert r.json()["flaeche"] is False and r.json()["rueckmeldung"] == "Richtig – gut gemacht."
    assert sperre.gesperrt() is False


def test_weiter_sperrt_wieder() -> None:
    naechster = turn(TASK_2, completed=1, revision=2)
    lern, client, sperre, penecho = _offene_sitzung({"session": naechster})
    client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert sperre.gesperrt() is False
    lern.antworten["learning_task_next"] = [{"session": naechster}]
    r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
    assert r.status_code == 200, r.text
    assert sperre.gesperrt() is True
    assert r.json()["nummer"] == 2 and r.json()["flaeche"] is True
    assert ("aufgabe_setzen", "pe-1", TASK_2, "Frage 2", 2) in penecho.aufrufe


def test_learning_weg_503_sperre_bleibt() -> None:
    lern, client, sperre, _ = _offene_sitzung(LearningWeg("x"))
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 503
    assert r.json() == {"meldung": NICHT_ERREICHBAR}
    assert sperre.gesperrt() is True
    # Auch eine freie Sperre bleibt frei.
    sperre.frei()
    lern.antworten["learning_task_next"] = LearningWeg("x")
    assert client.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 503
    assert sperre.gesperrt() is False


def test_doppelabgabe_409_laedt_stand() -> None:
    lern, client, sperre, _ = _offene_sitzung(LearningAbgelehnt("session revision conflict", "session_revision_conflict"))
    lern.antworten["learning_task_next"] = [{"session": turn(TASK_2, completed=1, revision=2)}]
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 409
    assert r.json()["meldung"] == SCHON_ABGEGEBEN and r.json()["code"] == "session_revision_conflict"
    assert r.json()["turn"]["task"]["id"] == TASK_2
    assert lern.tools().count("learning_task_answer") == 1
    assert lern.tools()[-1] == "learning_task_next"


def test_zustandskonflikt_409_schon_abgegeben() -> None:
    lern, client, _, _ = _offene_sitzung(LearningAbgelehnt("session is not active", "session_state_conflict"))
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 409
    assert r.json()["meldung"] == SCHON_ABGEGEBEN and r.json()["code"] == "session_state_conflict"


def test_andere_ablehnung_409_nicht_angenommen() -> None:
    lern, client, _, _ = _offene_sitzung(LearningAbgelehnt("answer format is invalid", "invalid_session_request"))
    lern.antworten["learning_task_next"] = [{"session": turn()}]
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 409
    assert r.json() == {"code": "invalid_session_request", "meldung": NICHT_ANGENOMMEN, "turn": turn()}
    assert lern.tools().count("learning_task_answer") == 1
    assert lern.tools()[-1] == "learning_task_next"


def test_zeichnung_canvas_konflikt_409_schon_abgegeben() -> None:
    lern, client, _, _ = _zeichen_sitzung()
    lern.antworten["learning_canvas_save"] = LearningAbgelehnt("PenEcho learning canvas revision conflict",
                                                               "canvas_revision_conflict")
    r = client.post(f"/lernen/api/sitzungen/{SID}/zeichnung", json={"revision": 1})
    assert r.status_code == 409
    assert r.json()["meldung"] == SCHON_ABGEGEBEN and r.json()["code"] == "canvas_revision_conflict"
    assert r.json()["turn"]["task"]["id"] == TASK_1
    assert lern.tools()[-1] == "learning_task_next"
    assert "learning_canvas_submit" not in lern.tools()


def test_zeichnung_andere_ablehnung_409_nicht_angenommen() -> None:
    lern, client, _, _ = _zeichen_sitzung()
    lern.antworten["learning_canvas_save"] = LearningAbgelehnt("canvas snapshot must be a PNG image",
                                                               "invalid_canvas_request")
    r = client.post(f"/lernen/api/sitzungen/{SID}/zeichnung", json={"revision": 1})
    assert r.status_code == 409
    assert r.json()["meldung"] == NICHT_ANGENOMMEN and r.json()["code"] == "invalid_canvas_request"
    assert "turn" in r.json() and "learning_canvas_submit" not in lern.tools()


def test_abgeschlossene_sitzung_gibt_frei() -> None:
    fertig = turn(None, completed=5, revision=6, state="completed",
                  feedback={"state": "correct", "score": 1.0, "rationale_codes": [], "source_refs": []})
    lern, client, sperre, _ = _offene_sitzung({"session": fertig})
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200
    assert r.json()["turn"]["task"] is None and r.json()["nummer"] is None
    assert sperre.gesperrt() is False
    # Auch beim Oeffnen: eine abgeschlossene Sitzung sperrt nicht.
    lern2 = FakeLern({"learning_course_overview": _ueberblick(turn()),
                      "learning_task_next": {"session": turn(None, state="completed")}})
    _, sperre2, penecho2, body = _offen(lern2)
    assert sperre2.gesperrt() is False and body["turn"]["task"] is None and body["flaeche"] is False
    assert "aufgabe_setzen" not in penecho2.namen()


def test_antwort_ungueltig_400() -> None:
    lern, client, _, _ = _offene_sitzung({"session": turn()})
    assert client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": "1", "answer": {}}).status_code == 400
    assert client.post(f"/lernen/api/sitzungen/{SID}/antwort", content=b"{kaputt").status_code == 400
    assert "learning_task_answer" not in lern.tools()


# ---- zeichnung ----------------------------------------------------------------

def _canvas(revision: int) -> dict[str, Any]:
    return {"canvas": {"id": SID, "revision": revision, "document": {
        "version": "1", "project_id": "55555555-5555-4555-8555-555555555555",
        "canvas_id": "66666666-6666-4666-8666-666666666666", "revision": revision,
        "background": "white", "objects": []}}}


def _zeichen_sitzung(penecho: FakePenEcho | None = None):
    zeichnen = turn(TASK_1, typ="penecho_canvas")
    naechster = turn(TASK_2, completed=1, revision=2)
    lern = FakeLern({
        "learning_course_overview": _ueberblick(zeichnen),
        "learning_task_next": {"session": zeichnen},
        "learning_progress_show": {"session": zeichnen},
        "learning_canvas_open": _canvas(4),
        "learning_canvas_save": _canvas(5),
        "learning_canvas_submit": {"session": naechster, "evaluation": {"score": 0.8, "accepted": True}},
    })
    client, sperre, penecho, _ = _offen(lern, penecho)
    return lern, client, sperre, penecho


def test_zeichnung_reihenfolge_open_save_submit() -> None:
    lern, client, sperre, penecho = _zeichen_sitzung()
    r = client.post(f"/lernen/api/sitzungen/{SID}/zeichnung", json={"revision": 1})
    assert r.status_code == 200, r.text
    tools = [t for t in lern.tools() if t.startswith("learning_canvas")]
    assert tools == ["learning_canvas_open", "learning_canvas_save", "learning_canvas_submit"]
    assert ("schnappschuss", "pe-1", 1) in penecho.aufrufe
    kw = dict(lern.aufrufe)
    offen = kw["learning_canvas_open"]
    assert offen["payload"]["activity_id"] == TASK_1
    ref = offen["payload"]["session_ref"]
    save = kw["learning_canvas_save"]
    assert save["expected_revision"] == 4 and save["schreibend"]
    assert save["payload"]["session_ref"] == ref
    assert base64.b64decode(save["payload"]["snapshot_png_base64"]) == PNG
    dokument = save["payload"]["document"]
    assert dokument["revision"] == 4 and len(dokument["objects"]) == 1
    assert dokument["objects"][0]["object_type"] == "image"
    from spaces.learning.contracts.penecho import CanvasSaveV1

    CanvasSaveV1.model_validate({"expected_revision": 4, "document": dokument})
    submit = kw["learning_canvas_submit"]
    assert submit["expected_revision"] == 5 and submit["confirmation"]["confirmed"] is True
    assert submit["payload"]["adaptive_session_revision"] == 1 and submit["payload"]["session_ref"] == ref
    assert sperre.gesperrt() is False
    assert r.json()["bewertung"] == {"punkte": 0.8, "angenommen": True}
    assert r.json()["turn"]["task"]["id"] == TASK_2


def test_zeichnung_ohne_flaeche_503() -> None:
    lern, client, sperre, penecho = _zeichen_sitzung()
    penecho.fehler = PenEchoNichtErreichbar("weg")
    r = client.post(f"/lernen/api/sitzungen/{SID}/zeichnung", json={"revision": 1})
    assert r.status_code == 503
    assert r.json() == {"meldung": FLAECHE_WEG}
    assert not any(t.startswith("learning_canvas") for t in lern.tools())
    assert sperre.gesperrt() is True


def test_zeichnung_veraltete_revision_409_ohne_abgabe() -> None:
    lern, client, _, _ = _zeichen_sitzung()
    r = client.post(f"/lernen/api/sitzungen/{SID}/zeichnung", json={"revision": 7})
    assert r.status_code == 409 and r.json()["meldung"] == SCHON_ABGEGEBEN
    assert not any(t.startswith("learning_canvas") for t in lern.tools())


def test_zeichnung_submit_abgelehnt_409() -> None:
    lern, client, _, _ = _zeichen_sitzung()
    lern.antworten["learning_canvas_submit"] = LearningAbgelehnt("stale", "session_revision_conflict")
    r = client.post(f"/lernen/api/sitzungen/{SID}/zeichnung", json={"revision": 1})
    assert r.status_code == 409 and r.json()["meldung"] == SCHON_ABGEGEBEN and "turn" in r.json()
    assert lern.tools().count("learning_canvas_submit") == 1


def test_zeichnung_wandelt_jpeg_in_png() -> None:
    from PIL import Image

    roh = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(roh, format="JPEG")
    lern, client, _, _ = _zeichen_sitzung(FakePenEcho(bild=roh.getvalue()))
    assert client.post(f"/lernen/api/sitzungen/{SID}/zeichnung", json={"revision": 1}).status_code == 200
    png = base64.b64decode(dict(lern.aufrufe)["learning_canvas_save"]["payload"]["snapshot_png_base64"])
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


# ---- erklaeren / hinweis --------------------------------------------------------

def test_erklaeren_ohne_quellenmarken() -> None:
    # Der Tutor belegt mit (S1), (S2, S3), "siehe S2", "aus S2"; die Huelle zeigt keine Quellen.
    lern, client, _, _ = _offene_sitzung({"session": turn()})
    lern.antworten["learning_tutor_ask"] = {"tutor": {
        "answer": "Ein Wendepunkt wechselt die Krümmung (S2). Es gilt f''(x)=0 (vgl. S1, S3). "
                  "Nun Schritt 4 (siehe S2): die pq-Formel aus S2 anwenden.",
        "next_step": "x", "source_refs": [], "evidence_ref": "e"}}
    r = client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={})
    assert r.json()["answer"] == ("Ein Wendepunkt wechselt die Krümmung. Es gilt f''(x)=0. "
                                  "Nun Schritt 4: die pq-Formel anwenden.")


def test_erklaeren_zeigt_next_step_nicht() -> None:
    # next_step schreibt das Modell mitunter an eine Lehrkraft, samt erwarteter Werte
    # ("p=-6, q=5 wird erwartet, aber nicht vorgeben"): nie an die lernende Person geben.
    lern, client, _, _ = _offene_sitzung({"session": turn()})
    lern.antworten["learning_tutor_ask"] = {"tutor": {
        "answer": "Welchen Wert hat p bei Ihnen?",
        "next_step": "Lassen Sie die Lernperson p benennen (p=-6 wird erwartet, aber nicht vorgeben).",
        "source_refs": [], "evidence_ref": "e"}}
    r = client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={})
    assert r.json() == {"answer": "Welchen Wert hat p bei Ihnen?"}
    assert "p=-6" not in r.text


def test_erklaeren_und_hinweis() -> None:
    lern, client, sperre, _ = _offene_sitzung({"session": turn()})
    lern.antworten["learning_tutor_ask"] = {"tutor": {"answer": "So geht es.", "next_step": "Weiter so.",
                                                      "source_refs": [], "evidence_ref": "e"}}
    lern.antworten["learning_hint_request"] = {"session": turn(), "hint": "Denken Sie an X."}
    r = client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={})
    assert r.json() == {"answer": "So geht es."}
    tutor = dict(lern.aufrufe)["learning_tutor_ask"]
    assert tutor["course_id"] == KURS and tutor["session_id"] == SID and tutor["payload"]["question"]
    assert client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={"frage": "x" * 501}).status_code == 400
    r = client.post(f"/lernen/api/sitzungen/{SID}/hinweis")
    assert r.json() == {"hinweis": "Denken Sie an X."}
    assert dict(lern.aufrufe)["learning_hint_request"]["payload"] == {"task_id": TASK_1}
    assert sperre.gesperrt() is True


# ---- Actor, Notizen, Bewertung ----------------------------------------------------

def test_actor_kommt_nur_aus_einstellung() -> None:
    umschlaege: list[dict[str, Any]] = []

    def mcp(request: httpx.Request) -> httpx.Response:
        event = json.loads(request.content)["params"]["arguments"]
        umschlaege.append(event)
        tool = json.loads(request.content)["params"]["name"]
        result = _ueberblick(turn()) if tool == "learning_course_overview" else {"session": turn()}
        text = json.dumps({"state": "completed", "result": result})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": text}], "isError": False}})

    api_koepfe: list[httpx.Headers] = []

    def api(request: httpx.Request) -> httpx.Response:
        api_koepfe.append(request.headers)
        return httpx.Response(200, json={"notes": []})

    lern = LernMcp("http://learning-mcp.test/mcp", E.actor_id, transport=httpx.MockTransport(mcp))
    http = httpx.Client(transport=httpx.MockTransport(api))
    client = TestClient(Bruecke(E, lern, FakePenEcho(), Sperre(), http))  # type: ignore[arg-type]
    boese = {"X-Learning-Actor": "boese-person", "X-LearnHouse-Learning-Service-Key": "x" * 40}
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen", headers=boese,
                       json={"actor": {"actor_id": "boese-person"}}).status_code == 200
    assert client.get(f"/lernen/api/notizen?course_id={KURS}&actor=boese-person", headers=boese).status_code == 200
    assert umschlaege and all(u["actor"] == {"actor_id": ACTOR, "actor_type": "local_user"} for u in umschlaege)
    assert api_koepfe[0]["X-Learning-Actor"] == ACTOR
    assert api_koepfe[0]["X-LearnHouse-Learning-Service-Key"] == SCHLUESSEL


def test_notizen_header_und_filter() -> None:
    gesehen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return httpx.Response(200, json={"notes": [
            {"id": "n1", "course_id": KURS, "quelle": "bewertung"},
            {"id": "n2", "course_id": FREMD, "quelle": "bewertung"},
        ]})

    client, _, _ = bauen(FakeLern({}), api=api)
    r = client.get(f"/lernen/api/notizen?course_id={KURS}")
    assert r.status_code == 200
    assert [n["id"] for n in r.json()["notes"]] == ["n1"]
    anfrage = gesehen[0]
    assert str(anfrage.url) == f"{API}/api/v1/notizen?quelle=bewertung"
    assert anfrage.headers["X-LearnHouse-Learning-Service-Key"] == SCHLUESSEL
    assert anfrage.headers["X-Learning-Actor"] == ACTOR


def test_notizen_api_weg_503() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("weg")

    client, _, _ = bauen(FakeLern({}), api=api)
    r = client.get(f"/lernen/api/notizen?course_id={KURS}")
    assert r.status_code == 503 and r.json() == {"meldung": NICHT_ERREICHBAR}


def test_bewertung_lesen_und_abgeben() -> None:
    gesehen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"bewertung": None})
        body = json.loads(request.content)
        if body["stichworte"] == ["x" * 41]:
            return httpx.Response(400, json={"code": "stichwort_zu_lang", "message": "Zu lang.", "retryable": False})
        return httpx.Response(200, json={"gespeichert": True, "neu": True})

    client, _, _ = bauen(FakeLern({}), api=api)
    assert client.get(f"/lernen/api/kurse/{KURS}/bewertung").json() == {"bewertung": None}
    assert str(gesehen[0].url) == f"{API}/api/v1/kursbewertung/{KURS}"
    r = client.post(f"/lernen/api/kurse/{KURS}/bewertung", json={"smiley": "gut", "stichworte": ["klar"]})
    assert r.status_code == 200 and r.json() == {"gespeichert": True, "neu": True}
    assert json.loads(gesehen[1].content) == {"course_id": KURS, "kurs_titel": "Testkurs", "smiley": "gut",
                                              "stichworte": ["klar"]}
    assert gesehen[1].headers["X-Learning-Actor"] == ACTOR
    assert client.post(f"/lernen/api/kurse/{KURS}/bewertung",
                       json={"smiley": "gut", "stichworte": ["a", "b", "c", "d"]}).status_code == 400
    assert client.post(f"/lernen/api/kurse/{KURS}/bewertung", json={"smiley": "super"}).status_code == 400
    r = client.post(f"/lernen/api/kurse/{KURS}/bewertung", json={"smiley": "gut", "stichworte": ["x" * 41]})
    assert r.status_code == 400 and r.json() == {"code": "stichwort_zu_lang", "meldung": "Zu lang."}
    assert len(gesehen) == 3


# ---- LernMcp ------------------------------------------------------------------

def _mcp_antwort(inhalt: dict[str, Any], *, fehler: bool = False, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": json.dumps(inhalt)}], "isError": fehler}})

    return httpx.MockTransport(handler)


def test_lern_mcp_umschlag_und_schluessel() -> None:
    gesendet: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesendet.append(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": json.dumps({"result": {"session": None}})}], "isError": False}})

    lern = LernMcp("http://m.test/mcp", ACTOR, transport=httpx.MockTransport(handler))
    for _ in range(2):
        lern.rufe("learning_task_answer", "learning.task.answer", session_id=SID, expected_revision=1,
                  payload={"answer": {"choice_id": "a"}}, schreibend=True)
    lern.rufe("learning_session_start", "learning.session.start", course_id=KURS, schreibend=True,
              payload={"course_revision": 1, "mode": "training", "blueprint": {"max_attempts": 20}})
    lern.rufe("learning_task_next", "learning.task.next", session_id=SID)
    a, b, start, lesen = (g["params"]["arguments"] for g in gesendet)
    assert a["idempotency_key"] == b["idempotency_key"]
    assert a["idempotency_key"].startswith("hu-") and len(a["idempotency_key"]) == 43
    ohne = {k: v for k, v in a.items() if k != "idempotency_key"}
    assert a["idempotency_key"] == idempotenz_schluessel("learning_task_answer", ohne)
    assert a["invocation_id"] != b["invocation_id"]
    assert start["idempotency_key"].startswith("hu-") and len(start["idempotency_key"]) == 35
    assert "idempotency_key" not in lesen
    from spaces.learning.contracts.mcp_models import EventEnvelopeV1

    for event in (a, start, lesen):
        EventEnvelopeV1.model_validate(event)


def test_lern_mcp_fehlerabbildung() -> None:
    with pytest.raises(LearningAbgelehnt) as abgelehnt:
        LernMcp("http://m.test/mcp", ACTOR, transport=_mcp_antwort(
            {"state": "rejected", "error": {"code": "session_revision_conflict", "message": "conflict"}}, fehler=True,
        )).rufe("learning_task_next", "learning.task.next")
    assert abgelehnt.value.nachricht == "conflict" and abgelehnt.value.code == "session_revision_conflict"
    with pytest.raises(LearningAbgelehnt):
        LernMcp("http://m.test/mcp", ACTOR, transport=_mcp_antwort({"error": "invalid_arguments"}, fehler=True)
                ).rufe("learning_task_next", "learning.task.next")
    with pytest.raises(LearningWeg):
        LernMcp("http://m.test/mcp", ACTOR, transport=_mcp_antwort(
            {"state": "unavailable", "error": {"code": "tutor_unavailable", "message": "x"}}, fehler=True,
        )).rufe("learning_tutor_ask", "learning.tutor.ask")
    with pytest.raises(LearningWeg):
        LernMcp("http://m.test/mcp", ACTOR, transport=_mcp_antwort({}, status=502)).rufe("learning_task_next", "x")

    def kaputt(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})

    with pytest.raises(LearningWeg):
        LernMcp("http://m.test/mcp", ACTOR, transport=httpx.MockTransport(kaputt)).rufe("learning_task_next", "x")

    def netz(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("weg")

    with pytest.raises(LearningWeg):
        LernMcp("http://m.test/mcp", ACTOR, transport=httpx.MockTransport(netz)).rufe("learning_task_next", "x")


# ---- Sperre ueber mehrere Kurse ---------------------------------------------------

KURS_B = "77777777-7777-4777-8777-777777777777"
SID_B = "88888888-8888-4888-8888-888888888888"
TASK_B = "44444444-4444-4444-8444-44444444444b"
E2 = einstellung_lesen({
    "LEARNING_HUELLE_BASIS": "https://lernen.example.test",
    "LEARNING_HUELLE_FLAECHE_BASIS": "https://lernen.example.test:8443",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNING_HUELLE_API_URL": API,
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
    "LEARNING_HUELLE_ACTOR": ACTOR,
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": KURS, "titel": "Testkurs"},
                                         {"course_id": KURS_B, "titel": "Kurs B"}]),
})


def turn_b(task_id: str | None = TASK_B, **kw: Any) -> dict[str, Any]:
    t = turn(task_id, course_id=KURS_B, **kw)
    t["session_id"] = SID_B
    return t


class ZweiKurse:
    """Learning mit zwei Kursen: Ueberblick und Turns je Kurs bzw. Sitzung."""

    def __init__(self, a: dict[str, Any] | Exception | None, b: dict[str, Any] | None) -> None:
        self.stand: dict[str, Any] = {KURS: a, KURS_B: b}
        self.antwort_b: dict[str, Any] | None = None

    def ueberblick(self, kw: dict[str, Any]) -> dict[str, Any]:
        stand = self.stand[kw["course_id"]]
        if isinstance(stand, Exception):
            raise stand
        return _ueberblick(stand if stand is not None and stand["state"] == "active" else None)

    def next(self, kw: dict[str, Any]) -> dict[str, Any]:
        stand = self.stand[KURS if kw["session_id"] == SID else KURS_B]
        if isinstance(stand, Exception):
            raise stand
        assert isinstance(stand, dict)
        return {"session": stand}

    def lern(self) -> FakeLern:
        return FakeLern({
            "learning_course_overview": self.ueberblick,
            "learning_task_next": self.next,
            "learning_session_start": lambda kw: {"session": self.stand[kw["course_id"]]},
            "learning_task_answer": lambda kw: {"session": self.antwort_b},
        })


def test_a_offen_b_fertig_bleibt_gesperrt() -> None:
    welt = ZweiKurse(turn(), turn_b(None, state="completed"))
    client, sperre, _ = bauen(welt.lern(), einstellung=E2)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    # B hat keine laufende Sitzung; der Start liefert hier eine schon fertige.
    r = client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen")
    assert r.status_code == 200 and r.json()["turn"]["task"] is None
    assert sperre.gesperrt() is True


def test_antwort_in_b_waehrend_a_offen_bleibt_gesperrt() -> None:
    welt = ZweiKurse(turn(), turn_b())
    client, sperre, _ = bauen(welt.lern(), einstellung=E2)
    client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen")
    welt.antwort_b = turn_b(TASK_2, completed=1, revision=2)
    r = client.post(f"/lernen/api/sitzungen/{SID_B}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200
    assert sperre.gesperrt() is True


def test_neustart_unbekannter_kurs_mit_offener_aufgabe_sperrt() -> None:
    # Nur B ist in diesem Prozess bekannt; A hat bei Learning eine offene Aufgabe.
    welt = ZweiKurse(turn(), turn_b(None, state="completed"))
    lern = welt.lern()
    client, sperre, _ = bauen(lern, einstellung=E2)
    sperre.frei()
    assert client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen").status_code == 200
    assert sperre.gesperrt() is True
    ueberblicke = [kw["course_id"] for t, kw in lern.aufrufe if t == "learning_course_overview"]
    assert ueberblicke == [KURS_B, KURS]


def test_uebung_in_a_haelt_die_sperre_nicht_zu() -> None:
    # A uebt (bekannte Sitzung), B ist eine Pruefung, deren Abgabe sie beendet: danach ist alles frei.
    welt = ZweiKurse(turn(mode="training"), turn_b())
    client, sperre, _ = bauen(welt.lern(), einstellung=E2)
    client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen")
    assert sperre.gesperrt() is True
    welt.antwort_b = turn_b(None, completed=5, revision=6, state="completed")
    r = client.post(f"/lernen/api/sitzungen/{SID_B}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200
    assert sperre.gesperrt() is False


def test_neustart_unbekannte_uebung_sperrt_nicht() -> None:
    welt = ZweiKurse(turn(mode="training"), turn_b(None, state="completed"))
    client, sperre, _ = bauen(welt.lern(), einstellung=E2)
    assert client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen").status_code == 200
    assert sperre.gesperrt() is False


def test_neustart_ueberblick_weg_bleibt_gesperrt() -> None:
    welt = ZweiKurse(LearningWeg("x"), turn_b(None, state="completed"))
    client, sperre, _ = bauen(welt.lern(), einstellung=E2)
    sperre.frei()
    assert client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen").status_code == 200
    assert sperre.gesperrt() is True


def test_alle_kurse_ohne_offene_aufgabe_frei() -> None:
    welt = ZweiKurse(None, turn_b())
    client, sperre, _ = bauen(welt.lern(), einstellung=E2)
    client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen")
    assert sperre.gesperrt() is True
    welt.antwort_b = turn_b(None, completed=5, revision=6, state="completed")
    r = client.post(f"/lernen/api/sitzungen/{SID_B}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200
    assert sperre.gesperrt() is False


# ---- Abschlusspruefung: Flaeche je Lernsitzung, veraltete Sitzungen, Bilder -------------

SID_2 = "99999999-9999-4999-8999-999999999999"


def _turn_in(sid: str, task_id: str | None = TASK_1, **kw: Any) -> dict[str, Any]:
    t = turn(task_id, **kw)
    t["session_id"] = sid
    return t


def test_flaeche_je_lernsitzung_nicht_je_kurs() -> None:
    welt = {"sid": SID}

    def ueberblick(kw: dict[str, Any]) -> dict[str, Any]:
        return _ueberblick(_turn_in(welt["sid"]))

    lern = FakeLern({"learning_course_overview": ueberblick,
                     "learning_task_next": lambda kw: {"session": _turn_in(kw["session_id"])}})
    client, _, penecho = bauen(lern)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    welt["sid"] = SID_2  # neue Lernsitzung desselben Kurses
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    schluessel = [a[1] for a in penecho.aufrufe if a[0] == "sitzung"]
    assert schluessel == [SID, SID_2]


def test_nummer_ausserhalb_der_flaeche_kein_absturz() -> None:
    weit = turn(TASK_1, completed=20)
    lern = FakeLern({"learning_course_overview": _ueberblick(weit), "learning_task_next": {"session": weit}})
    client, sperre, _ = bauen(lern)
    r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    assert r.status_code == 200 and r.json()["flaeche"] is False and r.json()["nummer"] == 21
    assert sperre.gesperrt() is True


def test_oeffnen_verwirft_alte_sitzung_des_kurses() -> None:
    # S1 hatte eine offene Aufgabe; danach laeuft bei Learning nur noch S2.
    welt: dict[str, Any] = {"aktiv": _turn_in(SID)}

    def next_(kw: dict[str, Any]) -> dict[str, Any]:
        if kw["session_id"] == SID and welt["aktiv"]["session_id"] != SID:
            return {"session": _turn_in(SID, None, state="abandoned")}
        return {"session": welt["aktiv"]}

    lern = FakeLern({
        "learning_course_overview": lambda kw: _ueberblick(welt["aktiv"]),
        "learning_task_next": next_,
        "learning_task_answer": lambda kw: {"session": _turn_in(SID_2, None, completed=5, revision=6,
                                                                state="completed")},
    })
    client, sperre, _ = bauen(lern)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    welt["aktiv"] = _turn_in(SID_2, TASK_2)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    vorher = len(lern.aufrufe)
    r = client.post(f"/lernen/api/sitzungen/{SID_2}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200
    assert sperre.gesperrt() is False
    # S1 wurde beim zweiten Oeffnen verworfen und nicht mehr gefragt.
    assert all(kw.get("session_id") != SID for _, kw in lern.aufrufe[vorher:])


def test_frei_liest_andere_sitzungen_bei_learning_neu() -> None:
    welt = ZweiKurse(turn(), turn_b())
    client, sperre, _ = bauen(welt.lern(), einstellung=E2)
    client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen")
    # A wurde anderswo (etwa im Fork) fertig; die Huelle hat noch die alte Aufgabe im Speicher.
    welt.stand[KURS] = turn(None, completed=5, revision=6, state="completed")
    welt.antwort_b = turn_b(None, completed=5, revision=6, state="completed")
    r = client.post(f"/lernen/api/sitzungen/{SID_B}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200
    assert sperre.gesperrt() is False


def test_frei_lesefehler_der_anderen_sitzung_bleibt_gesperrt() -> None:
    welt = ZweiKurse(turn(), turn_b())
    client, sperre, _ = bauen(welt.lern(), einstellung=E2)
    client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen")
    welt.stand[KURS] = LearningWeg("x")
    welt.antwort_b = turn_b(None, completed=5, revision=6, state="completed")
    r = client.post(f"/lernen/api/sitzungen/{SID_B}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 200
    assert sperre.gesperrt() is True


BILD = "term-0123456789abcdef.png"


def _mit_bildern(t: dict[str, Any]) -> dict[str, Any]:
    t["task"]["type"] = "matching"
    t["task"]["presentation"] = {
        "image": f"/lernsequenz-medien/{BILD}", "width": 640, "height": 360,
        "left": [{"id": "a", "label": "Graph 1", "image": "/lernsequenz-medien/graph-a.webp", "width": 4, "height": 3},
                 {"id": "b", "label": "Text"}],
        "right": [{"id": "s", "label": "f", "image": "https://evil.example/x.png"},
                  {"id": "t", "label": "g", "image": "/lernsequenz-medien/../x.png"}],
    }
    return t


def test_bilder_im_turn_zeigen_auf_eigene_route() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(_mit_bildern(turn())),
                     "learning_task_next": {"session": _mit_bildern(turn())}})
    _, _, _, body = _offen(lern)
    p = body["turn"]["task"]["presentation"]
    assert p["image"] == f"/lernen/api/medien/{BILD}" and p["width"] == 640
    assert p["left"][0]["image"] == "/lernen/api/medien/graph-a.webp"
    assert "image" not in p["left"][1]
    assert "image" not in p["right"][0] and "image" not in p["right"][1]


def test_bilder_auch_nach_antwort_und_409() -> None:
    naechster = _mit_bildern(turn(TASK_2, completed=1, revision=2))
    lern, client, _, _ = _offene_sitzung({"session": naechster})
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.json()["turn"]["task"]["presentation"]["image"] == f"/lernen/api/medien/{BILD}"
    lern.antworten["learning_task_answer"] = LearningAbgelehnt("stale", "session_revision_conflict")
    lern.antworten["learning_task_next"] = [{"session": _mit_bildern(turn(TASK_2, completed=1, revision=2))}]
    r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert r.status_code == 409
    assert r.json()["turn"]["task"]["presentation"]["image"] == f"/lernen/api/medien/{BILD}"


def test_medien_route_holt_bild_von_learning_api() -> None:
    gesehen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        if request.url.path == f"/lernsequenz-medien/{BILD}":
            return httpx.Response(200, content=PNG, headers={"content-type": "text/html"})
        return httpx.Response(404)

    client, _, _ = bauen(FakeLern({}), api=api)
    r = client.get(f"/lernen/api/medien/{BILD}")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert str(gesehen[0].url) == f"{API}/lernsequenz-medien/{BILD}"
    assert "X-LearnHouse-Learning-Service-Key" not in gesehen[0].headers
    assert client.get("/lernen/api/medien/fehlt-0000.jpg").status_code == 404
    anzahl = len(gesehen)
    for name in ("..%2Fx.png", ".x.png", "x.gif", "a" * 130 + ".png", "x.png.html"):
        assert client.get(f"/lernen/api/medien/{name}").status_code == 404, name
    assert len(gesehen) == anzahl


WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"0" * 8


def _medien_api(fehlt: frozenset[str] = frozenset()):
    def api(request: httpx.Request) -> httpx.Response:
        name = request.url.path.removeprefix("/lernsequenz-medien/")
        if name in fehlt:
            return httpx.Response(404)
        if name == BILD:
            return httpx.Response(200, content=PNG)
        if name == "graph-a.webp":
            return httpx.Response(200, content=WEBP)
        return httpx.Response(404)
    return api


def test_zeigen_gibt_aufgabenbilder_an_die_flaeche() -> None:
    # Abnahme 06.11.: die Zuordnungsaufgabe nannte vier Graphen, auf der Flaeche stand keiner.
    lern = FakeLern({"learning_course_overview": _ueberblick(_mit_bildern(turn())),
                     "learning_task_next": {"session": _mit_bildern(turn())}})
    _, _, penecho, _ = _offen(lern, api=_medien_api())
    assert penecho.bilder == [[Aufgabenbild(None, PNG, 640 / 360), Aufgabenbild("1) Graph 1", WEBP, 4 / 3)]]


def test_bildschild_traegt_das_etikett_vom_board() -> None:
    # Finaler Review (I1): links hiessen die Graphen "Graph A ...", auf dem Board aber 1, 2, ...; rechts
    # "Graph 1 ..." mit A, B, .... Auf dem Bild stand nur der Name: wer "3-B" nach den Bildern schrieb,
    # meinte andere Eintraege, als der Leser zurueckuebersetzte. Das Schild nennt jetzt das Board-Etikett.
    t = turn()
    t["task"]["type"] = "matching"
    graph = {"image": "/lernsequenz-medien/graph-a.webp", "width": 4, "height": 3}
    t["task"]["presentation"] = {
        "left": [{"id": "l1", "label": "Graph A", **graph}, {"id": "l2", "label": "Graph B", **graph}],
        "right": [{"id": "r1", "label": "Graph 1", **graph}, {"id": "r2", "label": "Graph 2", **graph}],
    }
    lern = FakeLern({"learning_course_overview": _ueberblick(t), "learning_task_next": {"session": t}})
    _, _, penecho, _ = _offen(lern, api=_medien_api())
    assert [b.beschriftung for b in penecho.bilder[0]] == ["1) Graph A", "2) Graph B", "A) Graph 1", "B) Graph 2"]


def test_fehlendes_aufgabenbild_wird_ausgelassen() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(_mit_bildern(turn())),
                     "learning_task_next": {"session": _mit_bildern(turn())}})
    _, _, penecho, _ = _offen(lern, api=_medien_api(frozenset({BILD})))
    assert penecho.bilder == [[Aufgabenbild("1) Graph 1", WEBP, 4 / 3)]]


def test_learning_weg_aufgabe_kommt_ohne_bilder() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("weg")

    lern = FakeLern({"learning_course_overview": _ueberblick(_mit_bildern(turn())),
                     "learning_task_next": {"session": _mit_bildern(turn())}})
    _, _, penecho, body = _offen(lern, api=api)
    assert penecho.bilder == [[]] and body["turn"]["task"]["id"] == TASK_1


def test_medien_route_learning_weg_503() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("weg")

    client, _, _ = bauen(FakeLern({}), api=api)
    r = client.get(f"/lernen/api/medien/{BILD}")
    assert r.status_code == 503


# ---- Kursende: abgeschlossenen Durchlauf zeigen statt stumm neu starten -------------

ENDE = turn(None, completed=5, revision=9, state="completed")


def _bis_zum_ende(**kw: Any):
    lern = FakeLern({
        "learning_course_overview": _ueberblick(turn()),
        "learning_task_next": [{"session": turn()}],
        "learning_session_start": {"session": turn(TASK_2)},
    })
    client, sperre, penecho = bauen(lern, **kw)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    lern.antworten["learning_task_next"] = [{"session": ENDE}]
    r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
    assert r.status_code == 200 and r.json()["turn"]["state"] == "completed"
    # Learning kennt danach keine aktive Sitzung mehr.
    lern.antworten["learning_course_overview"] = _ueberblick(None)
    return lern, client, sperre


def test_kursende_zeigt_abgeschlossenen_durchlauf_statt_neustart() -> None:
    lern, client, sperre = _bis_zum_ende()
    vorher = len(lern.aufrufe)
    r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["abgeschlossen"] is True and body["turn"]["state"] == "completed"
    neu = lern.tools()[vorher:]
    assert "learning_session_start" not in neu
    assert ("learning_task_next", {"session_id": SID}) in lern.aufrufe[vorher:]
    assert sperre.gesperrt() is False


def test_neu_startet_frischen_durchlauf() -> None:
    lern, client, sperre = _bis_zum_ende()
    lern.antworten["learning_task_next"] = [{"session": turn(TASK_2)}]
    r = client.post(f"/lernen/api/kurse/{KURS}/neu")
    assert r.status_code == 200, r.text
    assert "learning_session_start" in lern.tools()
    assert r.json()["turn"]["task"]["id"] == TASK_2 and r.json()["abgeschlossen"] is False
    assert sperre.gesperrt() is True
    # Danach zaehlt wieder der neue Durchlauf, nicht das alte Ende.
    lern.antworten["learning_course_overview"] = _ueberblick(turn(TASK_2))
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()["abgeschlossen"] is False


def test_kursende_ueberlebt_neustart_der_huelle(tmp_path) -> None:
    datei = tmp_path / "kursende.json"
    lern, _, _ = _bis_zum_ende(kursende=datei)
    assert json.loads(datei.read_text(encoding="utf-8")) == {f"{ACTOR}|{KURS}": SID}
    lern.antworten["learning_task_next"] = [{"session": ENDE}]
    client2, _, _ = bauen(lern, kursende=datei)
    assert client2.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()["abgeschlossen"] is True


def test_kursende_einer_fremden_kennung_gilt_nicht(tmp_path) -> None:
    datei = tmp_path / "kursende.json"
    datei.write_text(json.dumps({f"lh-1-andere|{KURS}": SID}), encoding="utf-8")
    lern = FakeLern({"learning_course_overview": _ueberblick(None),
                     "learning_session_start": {"session": turn()}, "learning_task_next": {"session": turn()}})
    client, _, _ = bauen(lern, kursende=datei)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()["abgeschlossen"] is False
    assert "learning_session_start" in lern.tools()


def test_unlesbares_kursende_wird_vergessen() -> None:
    lern, client, _ = _bis_zum_ende()
    lern.antworten["learning_task_next"] = [LearningAbgelehnt("weg", "session_access_denied"), {"session": turn()}]
    lern.antworten["learning_session_start"] = {"session": turn()}
    r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    assert r.status_code == 200 and r.json()["abgeschlossen"] is False
    assert "learning_session_start" in lern.tools()


# ---- Rueckfrage an den Tutor ---------------------------------------------------------

def _tutor(antwort: str) -> dict[str, Any]:
    return {"tutor": {"answer": antwort, "next_step": "Rechnen Sie.", "source_refs": [], "evidence_ref": "e"}}


def test_rueckfrage_traegt_das_bisherige_gespraech() -> None:
    lern, client, _, _ = _offene_sitzung({"session": turn()})
    lern.antworten["learning_tutor_ask"] = [_tutor("Bilden Sie zuerst f'."), _tutor("Fast: der Faktor fehlt.")]
    client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={})
    r = client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={"frage": "Ich habe f'(x) = x² raus."})
    assert r.status_code == 200 and r.json()["answer"] == "Fast: der Faktor fehlt."
    fragen = [kw["payload"]["question"] for t, kw in lern.aufrufe if t == "learning_tutor_ask"]
    assert "Bilden Sie zuerst f'." in fragen[1] and "Ich habe f'(x) = x² raus." in fragen[1]
    assert fragen[1].rstrip().endswith("Ich habe f'(x) = x² raus.")
    # "Erklär mir das" faengt neu an.
    client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={})
    fragen = [kw["payload"]["question"] for t, kw in lern.aufrufe if t == "learning_tutor_ask"]
    assert fragen[2] == "Erklär mir das."


def test_rueckfrage_vergisst_das_gespraech_bei_neuer_aufgabe() -> None:
    lern, client, _, _ = _offene_sitzung({"session": turn(TASK_2, completed=1, revision=2)})
    lern.antworten["learning_tutor_ask"] = [_tutor("Zu Aufgabe 1."), _tutor("Zu Aufgabe 2.")]
    client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={})
    client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    lern.antworten["learning_task_next"] = [{"session": turn(TASK_2, completed=1, revision=2)}]
    client.post(f"/lernen/api/sitzungen/{SID}/weiter")
    client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={"frage": "Und jetzt?"})
    letzte = [kw["payload"]["question"] for t, kw in lern.aufrufe if t == "learning_tutor_ask"][-1]
    assert "Zu Aufgabe 1." not in letzte and letzte == "Und jetzt?"


# ---- Flaeche zuerst: abgeben, korrektur, passt (Vorbewertung) -------------------------

VOR_ID = "55555555-5555-4555-8555-555555555551"
VOR_ID_2 = "55555555-5555-4555-8555-555555555552"


def _vor(*, lesbar: bool = True, score: float | None = 1.0, gelesen: str | None = "-1,5",
         art: str = "deterministic", quelle: str = "handschrift", criteria: list[dict[str, Any]] | None = None,
         vid: str = VOR_ID) -> dict[str, Any]:
    feedback = None
    if lesbar:
        # Baender wie session_service._turn_feedback (angenommene Bewertung).
        s = score or 0.0
        zustand = "correct" if s >= 0.999 else ("partial" if s >= 0.65 else "incorrect")
        feedback = {"state": zustand, "score": score, "rationale_codes": [], "source_refs": [],
                    "criteria": criteria or []}
    return {"id": vid, "attempt_id": "77777777-7777-4777-8777-777777777771", "quelle": quelle, "lesbar": lesbar,
            "gelesen_text": gelesen, "art": art, "score": score if lesbar else None,
            "accepted": (score == 1) if lesbar else None, "feedback": feedback}


def _naechster() -> dict[str, Any]:
    return turn(TASK_2, completed=1, revision=3, mode="training",
                feedback={"state": "correct", "score": 1.0, "rationale_codes": [], "source_refs": []})


def _flaechen_sitzung(typ: str = "numeric", *, penecho: FakePenEcho | None = None,
                      vor: dict[str, Any] | None = None, confirm: Any = None, **kw: Any):
    offen = turn(TASK_1, typ=typ, mode="training")
    lern = FakeLern({
        "learning_course_overview": _ueberblick(offen),
        "learning_task_next": {"session": offen},
        "learning_task_preview": {"session": offen, "vorbewertung": vor or _vor()},
        "learning_task_confirm": confirm if confirm is not None else {"session": _naechster()},
    })
    client, sperre, penecho, _ = _offen(lern, penecho, **kw)
    return lern, client, sperre, penecho


def _aufrufe(lern: FakeLern, tool: str) -> list[dict[str, Any]]:
    return [kw for t, kw in lern.aufrufe if t == tool]


def _karten(penecho: FakePenEcho) -> list[tuple[Any, ...]]:
    return [a for a in penecho.aufrufe if a[0] == "rueckmeldung_zeigen"]


def test_abgeben_numeric_nimmt_ergebnisfeld_und_zeigt_karte() -> None:
    lern, client, sperre, penecho = _flaechen_sitzung("numeric")
    vorher = len(penecho.aufrufe)
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 200, r.text
    (kw,) = _aufrufe(lern, "learning_task_preview")
    assert kw["session_id"] == SID and kw["expected_revision"] == 1 and kw["schreibend"] is True
    assert set(kw["payload"]) == {"handschrift_png_base64"}
    assert base64.b64decode(kw["payload"]["handschrift_png_base64"]) == PNG
    neu = penecho.aufrufe[vorher:]
    assert ("ergebnis_schnappschuss", "pe-1", 1) in neu and "schnappschuss" not in [a[0] for a in neu]
    (karte,) = _karten(penecho)
    assert karte[:6] == ("rueckmeldung_zeigen", "pe-1", TASK_1, 1, "-1,5", "Richtig.")
    namen = [a[0] for a in neu]
    assert "dokument_merken" in namen and namen.index("dokument_merken") > namen.index("rueckmeldung_zeigen")
    body = r.json()
    assert body["turn"]["session_revision"] == 1 and body["turn"]["task"]["id"] == TASK_1
    assert body["vorbewertung"]["id"] == VOR_ID and body["flaeche"] is True and body["urteil"] == "Richtig."
    assert "learning_task_answer" not in lern.tools() and "learning_task_confirm" not in lern.tools()
    assert sperre.gesperrt() is False  # Ueben bleibt KI-frei


def test_abgeben_open_nimmt_ganzen_bereich() -> None:
    kriterium = {"criterion_id": "c1", "description": "Nennt die Ursache", "score": 0.5,
                 "feedback": "Die Ursache fehlt (S1)."}
    lern, client, _, penecho = _flaechen_sitzung(
        "open", vor=_vor(score=0.8, gelesen=None, art="rubric", criteria=[kriterium]))
    vorher = len(penecho.aufrufe)
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 200, r.text
    neu = [a[0] for a in penecho.aufrufe[vorher:]]
    assert "schnappschuss" in neu and "ergebnis_schnappschuss" not in neu
    (karte,) = _karten(penecho)
    assert karte[4] is None and karte[5] == "Teilweise richtig."
    assert "Nennt die Ursache: Die Ursache fehlt." in karte[6] and "S1" not in karte[6]
    assert base64.b64decode(_aufrufe(lern, "learning_task_preview")[0]["payload"]["handschrift_png_base64"]) == PNG


@pytest.mark.parametrize("typ", ["case", "penecho_canvas"])
def test_abgeben_case_und_zeichnung_nehmen_den_bereich(typ: str) -> None:
    _, client, _, penecho = _flaechen_sitzung(typ, vor=_vor(gelesen=None, art="rubric"))
    vorher = len(penecho.aufrufe)
    assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1}).status_code == 200
    assert "schnappschuss" in [a[0] for a in penecho.aufrufe[vorher:]]


def test_abgeben_code_400() -> None:
    lern, client, _, penecho = _flaechen_sitzung("code")
    vorher = len(penecho.aufrufe)
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 400
    assert r.json()["meldung"] == KEINE_FLAECHENAUFGABE == "Diese Aufgabe beantworten Sie in der Seitenleiste."
    assert "learning_task_preview" not in lern.tools()
    assert not any("schnappschuss" in a[0] for a in penecho.aufrufe[vorher:])


def test_abgeben_ohne_flaeche_503() -> None:
    lern, client, _, penecho = _flaechen_sitzung("numeric")
    penecho.fehler = PenEchoNichtErreichbar("weg")
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 503 and r.json() == {"meldung": FLAECHE_WEG}
    assert "learning_task_preview" not in lern.tools()


def test_abgeben_ungueltig_400() -> None:
    lern, client, _, _ = _flaechen_sitzung("numeric")
    for body in ({}, {"revision": "1"}, {"revision": True}, {"revision": 0}):
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json=body).status_code == 400, body
    assert "learning_task_preview" not in lern.tools()


def test_abgeben_veraltete_karte_409_mit_turn() -> None:
    lern, client, _, penecho = _flaechen_sitzung("numeric")
    vorher = len(penecho.aufrufe)
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 7})
    assert r.status_code == 409 and r.json()["meldung"] == SCHON_ABGEGEBEN
    assert r.json()["turn"]["task"]["id"] == TASK_1
    assert "learning_task_preview" not in lern.tools()
    assert not any("schnappschuss" in a[0] for a in penecho.aufrufe[vorher:])
    # Auch wenn erst Learning die Revision ablehnt.
    lern.antworten["learning_task_preview"] = LearningAbgelehnt("conflict", "session_revision_conflict")
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 409 and r.json()["code"] == "session_revision_conflict" and "turn" in r.json()
    assert _karten(penecho) == []


def test_abgeben_bewertung_nicht_erreichbar_503() -> None:
    lern, client, _, penecho = _flaechen_sitzung("numeric")
    lern.antworten["learning_task_preview"] = LearningWeg("learning_task_preview", code="session_grading_unavailable")
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 503
    assert r.json() == {"meldung": BEWERTUNG_WEG, "code": "session_grading_unavailable"}
    assert "Bewertung" in BEWERTUNG_WEG and "noch einmal" in BEWERTUNG_WEG
    assert _karten(penecho) == []
    # Ohne Code bleibt es die allgemeine Meldung.
    lern.antworten["learning_task_preview"] = LearningWeg("learning_task_preview")
    assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1}).json() == {
        "meldung": NICHT_ERREICHBAR}


def test_vorbewertung_in_pruefung_403() -> None:
    lern, client, _, _ = _flaechen_sitzung("numeric")
    verweigert = LearningAbgelehnt("exam sessions have no preview grading", "session_access_denied")
    lern.antworten["learning_task_preview"] = verweigert
    lern.antworten["learning_task_confirm"] = verweigert
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 403 and r.json() == {"code": "session_access_denied", "meldung": NUR_UEBEN}
    r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"revision": 1, "vorbewertung_id": VOR_ID})
    assert r.status_code == 403 and r.json()["meldung"] == NUR_UEBEN
    r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur",
                    json={"revision": 1, "vorbewertung_id": VOR_ID, "answer": {"value": 1}})
    assert r.status_code == 403


def test_abgeben_unlesbar_karte_ohne_gelesen_und_passt_400() -> None:
    lern, client, _, penecho = _flaechen_sitzung(
        "numeric", vor=_vor(lesbar=False, gelesen="Kritzel"),
        confirm=LearningAbgelehnt("vorbewertung is not readable", "invalid_session_request"))
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 200, r.text
    (karte,) = _karten(penecho)
    assert karte[4] is None and karte[5] == "Nicht lesbar."
    assert "Kritzel" not in karte[6] and "tippen Sie es selbst ein" in karte[6]
    assert r.json()["vorbewertung"]["lesbar"] is False and r.json()["urteil"] == "Nicht lesbar."
    r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"revision": 1, "vorbewertung_id": VOR_ID})
    assert r.status_code == 400
    assert r.json() == {"code": "invalid_session_request", "meldung": NICHT_BESTAETIGT}


def test_korrektur_schickt_korrigiert_aus() -> None:
    lern, client, _, penecho = _flaechen_sitzung("numeric")
    lern.antworten["learning_task_preview"] = {
        "session": turn(TASK_1, typ="numeric", mode="training"),
        "vorbewertung": _vor(quelle="getippt", gelesen=None, vid=VOR_ID_2),
    }
    vorher = len(penecho.aufrufe)
    r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur",
                    json={"revision": 1, "vorbewertung_id": VOR_ID, "answer": {"value": -1.5}})
    assert r.status_code == 200, r.text
    (kw,) = _aufrufe(lern, "learning_task_preview")
    assert kw["payload"] == {"answer": {"value": -1.5}, "korrigiert_aus": VOR_ID}
    assert kw["expected_revision"] == 1 and kw["schreibend"] is True
    neu = [a[0] for a in penecho.aufrufe[vorher:]]
    assert not any("schnappschuss" in n for n in neu)
    (karte,) = _karten(penecho)
    assert karte[2:6] == (TASK_1, 1, None, "Richtig.")
    assert r.json()["vorbewertung"]["id"] == VOR_ID_2 and r.json()["turn"]["session_revision"] == 1
    assert r.json()["flaeche"] is True and "dokument_merken" in neu


def test_korrektur_ungueltig_und_nur_fuer_deterministische_typen() -> None:
    lern, client, _, _ = _flaechen_sitzung("numeric")
    for body in ({"revision": 1, "answer": {"value": 1}},
                 {"revision": 1, "vorbewertung_id": VOR_ID},
                 {"revision": 1, "vorbewertung_id": VOR_ID, "answer": "x"},
                 {"revision": 1, "vorbewertung_id": 5, "answer": {"value": 1}},
                 {"vorbewertung_id": VOR_ID, "answer": {"value": 1}}):
        assert client.post(f"/lernen/api/sitzungen/{SID}/korrektur", json=body).status_code == 400, body
    assert "learning_task_preview" not in lern.tools()
    lern2, client2, _, _ = _flaechen_sitzung("open", vor=_vor(gelesen=None, art="rubric"))
    r = client2.post(f"/lernen/api/sitzungen/{SID}/korrektur",
                     json={"revision": 1, "vorbewertung_id": VOR_ID, "answer": {"text": "x"}})
    assert r.status_code == 400 and r.json()["meldung"] == KEINE_KORREKTUR
    assert "learning_task_preview" not in lern2.tools()


def test_korrektur_falsches_format_400_und_veraltet_409() -> None:
    lern, client, _, _ = _flaechen_sitzung("numeric")
    lern.antworten["learning_task_preview"] = LearningAbgelehnt("answer format is invalid", "invalid_session_request")
    body = {"revision": 1, "vorbewertung_id": VOR_ID, "answer": {"value": "x"}}
    r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur", json=body)
    assert r.status_code == 400 and r.json()["meldung"] == NICHT_ANGENOMMEN
    lern.antworten["learning_task_preview"] = LearningAbgelehnt("conflict", "session_state_conflict")
    r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur", json=body)
    assert r.status_code == 409 and r.json()["meldung"] == SCHON_ABGEGEBEN and "turn" in r.json()
    r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur", json={**body, "revision": 4})
    assert r.status_code == 409 and r.json()["code"] == "session_revision_conflict"


def test_passt_bestaetigt_und_merkt_naechste() -> None:
    lern, client, sperre, penecho = _flaechen_sitzung("numeric")
    nachher = _ueberblick(_naechster())
    nachher["begriffe"] = [{"concept_key": "k1", "titel": "Begriff", "beherrschung": 0.7}]
    lern.antworten["learning_course_overview"] = nachher
    vorher = len(penecho.aufrufe)
    r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"revision": 1, "vorbewertung_id": VOR_ID})
    assert r.status_code == 200, r.text
    (kw,) = _aufrufe(lern, "learning_task_confirm")
    assert kw["payload"] == {"vorbewertung_id": VOR_ID}
    assert kw["session_id"] == SID and kw["expected_revision"] == 1 and kw["schreibend"] is True
    body = r.json()
    assert body["nummer"] == 2 and body["turn"]["task"]["id"] == TASK_2
    assert body["begriffe"] == [{"concept_key": "k1", "titel": "Begriff", "beherrschung": 0.7}]
    assert body["rueckmeldung"] is None
    neu = [a[0] for a in penecho.aufrufe[vorher:]]
    assert "rueckmeldung_zeigen" not in neu and "rueckmeldung_setzen" not in neu and "aufgabe_setzen" not in neu
    assert "dokument_merken" in neu
    assert sperre.gesperrt() is False
    # weiter zeigt danach die vorgemerkte Aufgabe.
    lern.antworten["learning_task_next"] = {"session": _naechster()}
    r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
    assert r.status_code == 200 and r.json()["nummer"] == 2
    assert (TASK_2, 2, True) in penecho.ergebnisfelder


def test_passt_doppelt_409_mit_turn() -> None:
    lern, client, _, _ = _flaechen_sitzung(
        "numeric", confirm=[{"session": _naechster()},
                            LearningAbgelehnt("vorbewertung not current", "session_state_conflict")])
    body = {"revision": 1, "vorbewertung_id": VOR_ID}
    assert client.post(f"/lernen/api/sitzungen/{SID}/passt", json=body).status_code == 200
    lern.antworten["learning_task_next"] = {"session": _naechster()}
    r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json=body)
    assert r.status_code == 409
    assert r.json()["code"] == "session_state_conflict" and r.json()["meldung"] == SCHON_ABGEGEBEN
    assert r.json()["turn"]["task"]["id"] == TASK_2
    assert lern.tools().count("learning_task_confirm") == 2
    assert lern.tools()[-1] == "learning_task_next"


def test_passt_veraltete_revision_409_mit_turn() -> None:
    lern, client, _, _ = _flaechen_sitzung(
        "numeric", confirm=LearningAbgelehnt("adaptive session revision conflict", "session_revision_conflict"))
    r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"revision": 1, "vorbewertung_id": VOR_ID})
    assert r.status_code == 409 and r.json()["meldung"] == SCHON_ABGEGEBEN
    assert r.json()["turn"]["task"]["id"] == TASK_1


def test_passt_ungueltig_400() -> None:
    lern, client, _, _ = _flaechen_sitzung("numeric")
    for body in ({"revision": 1}, {"revision": 1, "vorbewertung_id": ""}, {"vorbewertung_id": VOR_ID},
                 {"revision": 1, "vorbewertung_id": ["x"]}):
        assert client.post(f"/lernen/api/sitzungen/{SID}/passt", json=body).status_code == 400, body
    assert "learning_task_confirm" not in lern.tools()


def test_passt_nach_neustart_der_bruecke() -> None:
    lern, client, _, _ = _flaechen_sitzung("numeric")
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 200
    vid = r.json()["vorbewertung"]["id"]
    # Neue Bruecke-Instanz: nichts im Speicher, die Kennung kommt nur aus dem Body.
    client2, _, penecho2 = bauen(lern)
    vorher = len(lern.aufrufe)
    r = client2.post(f"/lernen/api/sitzungen/{SID}/passt", json={"revision": 1, "vorbewertung_id": vid})
    assert r.status_code == 200, r.text
    neu = lern.aufrufe[vorher:]
    assert neu[0] == ("learning_task_next", {"session_id": SID})
    (kw,) = [k for t, k in neu if t == "learning_task_confirm"]
    assert kw["session_id"] == SID and kw["expected_revision"] == 1
    assert kw["payload"] == {"vorbewertung_id": VOR_ID} and kw["schreibend"] is True
    assert r.json()["nummer"] == 2
    # Kein Board in dieser Instanz bekannt: nichts gemerkt, keine Sitzung angelegt (merkt `weiter`).
    assert "dokument_merken" not in penecho2.namen() and "sitzung" not in penecho2.namen()


def test_passt_am_kursende_merkt_das_ende(tmp_path) -> None:
    datei = tmp_path / "kursende.json"
    fertig = turn(None, completed=5, revision=9, state="completed", mode="training")
    _, client, _, _ = _flaechen_sitzung("numeric", confirm={"session": fertig}, kursende=datei)
    r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"revision": 1, "vorbewertung_id": VOR_ID})
    assert r.status_code == 200 and r.json()["nummer"] is None
    assert json.loads(datei.read_text(encoding="utf-8")) == {f"{ACTOR}|{KURS}": SID}


def test_merken_fehler_blockiert_nichts(caplog: pytest.LogCaptureFixture) -> None:
    lern, client, _, penecho = _flaechen_sitzung("numeric")
    penecho.merken_fehler = PenEchoNichtErreichbar("geheim -1,5")
    with caplog.at_level("DEBUG"):
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1}).status_code == 200
        assert client.post(f"/lernen/api/sitzungen/{SID}/passt",
                           json={"revision": 1, "vorbewertung_id": VOR_ID}).status_code == 200
        lern.antworten["learning_task_next"] = {"session": _naechster()}
        assert client.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 200
    assert penecho.namen().count("dokument_merken") == 3
    assert "geheim" not in caplog.text and "-1,5" not in caplog.text
    assert base64.b64encode(PNG).decode("ascii") not in caplog.text


def test_weiter_merkt_das_board_still() -> None:
    naechster = turn(TASK_2, completed=1, revision=2, mode="training")
    lern, client, _, penecho = _offene_sitzung({"session": naechster})
    lern.antworten["learning_task_next"] = [{"session": naechster}]
    client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})
    assert "dokument_merken" not in penecho.namen()  # die alte Route bleibt, wie sie ist
    r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
    assert r.status_code == 200
    namen = penecho.namen()
    assert namen.index("dokument_merken") > max(i for i, n in enumerate(namen) if n == "aufgabe_setzen")


@pytest.mark.parametrize("typ, mit", [
    ("single_choice", True), ("multiple_choice", True), ("matching", True), ("ordering", True),
    ("numeric", True), ("fill_in", True), ("open", False), ("case", False), ("penecho_canvas", False),
    ("code", False),
])
def test_zeigen_setzt_ergebnisfeld_nur_fuer_deterministische_typen(typ: str, mit: bool) -> None:
    offen = turn(TASK_1, typ=typ, mode="training")
    offen["task"]["prompt"] = "Was gilt für $x^2$?"
    if typ in ("single_choice", "multiple_choice", "ordering"):
        offen["task"]["options"] = [{"id": "o1", "label": "Eins"}, {"id": "o2", "label": "Zwei"}]
    lern = FakeLern({"learning_course_overview": _ueberblick(offen), "learning_task_next": {"session": offen}})
    _, _, penecho, _ = _offen(lern)
    assert penecho.ergebnisfelder == [(TASK_1, 1, mit)]
    # Offene und Zeichenaufgaben bekommen ein sichtbares Arbeitsfeld, code bleibt in der Seitenleiste.
    assert penecho.arbeitsfelder == [(TASK_1, 1, typ in ("open", "case", "penecho_canvas"))]
    (gesetzt,) = [a for a in penecho.aufrufe if a[0] == "aufgabe_setzen"]
    text = gesetzt[3]
    if typ == "code":
        assert text == "Was gilt für $x^2$?"  # wie bisher: nur der Prompt
    else:
        # aufgabe_setzen stellt selbst „Aufgabe {nummer}: “ voran; zusammen ergibt das genau aufgabentext.
        assert "Aufgabe 1: " + text == aufgabentext(offen["task"], 1)
        assert not text.startswith("Aufgabe") and "$x^2$" in text  # Formel bleibt als LaTeX
    if typ in ("single_choice", "multiple_choice", "ordering"):
        assert "A) Eins" in text.splitlines() and "B) Zwei" in text.splitlines()


@pytest.mark.parametrize("vor, erwartet", [
    # _vor setzt feedback.state mit Learnings Baendern (>= 0.999 correct, >= 0.65 partial).
    (_vor(score=1.0), "Richtig."),
    (_vor(score=0.9995), "Richtig."),
    (_vor(score=0.8), "Teilweise richtig."),
    (_vor(score=0.5), "Noch nicht richtig."),  # Learning: incorrect, wie der Turn nach „Passt“
    (_vor(score=0.0), "Noch nicht richtig."),
    (_vor(lesbar=False), "Nicht lesbar."),
    ({"lesbar": False, "score": 1.0, "accepted": True, "feedback": {"state": "correct"}}, "Nicht lesbar."),
    ({}, "Nicht lesbar."),
])
def test_urteil_aus_score(vor: dict[str, Any], erwartet: str) -> None:
    assert urteil_aus(vor) == erwartet
    assert erwartet in URTEILE


@pytest.mark.parametrize("zustand, score, erwartet", [
    ("correct", 0.2, "Richtig."),            # nur der Zustand zaehlt, nicht die Punkte
    ("partial", 1.0, "Teilweise richtig."),
    ("incorrect", 0.9, "Noch nicht richtig."),
    ("needs_review", 1.0, "Wird noch geprüft."),
])
def test_urteil_je_zustand_von_learning(zustand: str, score: float, erwartet: str,
                                        caplog: pytest.LogCaptureFixture) -> None:
    vor = {"lesbar": True, "score": score, "accepted": zustand != "needs_review",
           "feedback": {"state": zustand, "score": score}}
    with caplog.at_level("WARNING", logger="spaces.learning.huelle.bruecke"):
        assert urteil_aus(vor) == erwartet
    assert erwartet in URTEILE
    assert caplog.messages == []  # bekannte Zustaende sind kein Fehler


def test_abgeben_offene_pruefung_karte_wird_noch_geprueft() -> None:
    kriterium = {"criterion_id": "c1", "description": "Begruendet", "score": 0.4, "feedback": "Begruendung fehlt."}
    vor = _vor(score=0.4, gelesen=None, art="rubric", criteria=[kriterium])
    vor["accepted"], vor["feedback"]["state"] = False, "needs_review"
    _, client, _, penecho = _flaechen_sitzung("open", vor=vor)
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 200 and r.json()["urteil"] == "Wird noch geprüft."
    (karte,) = _karten(penecho)
    assert karte[5] == "Wird noch geprüft."
    assert "Begruendet: Begruendung fehlt." in karte[6] and WIRD_GEPRUEFT in karte[6]
    assert "überarbeiten" not in karte[6]


@pytest.mark.parametrize("feedback, geloggt", [
    ({"state": "neu_erfunden"}, "neu_erfunden"),
    ({"state": "Geheim -1,5 <x>"}, "str"),
    ({}, "NoneType"),
    (None, "NoneType"),
])
def test_urteil_unbekannter_zustand_noch_nicht_richtig_und_geloggt(
    feedback: dict[str, Any] | None, geloggt: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="spaces.learning.huelle.bruecke"):
        assert urteil_aus({"lesbar": True, "score": 1.0, "accepted": False, "feedback": feedback}) == (
            "Noch nicht richtig.")
    assert caplog.messages == [f"Vorbewertung ohne bekanntes Urteil: {geloggt}"]
    assert "Geheim" not in caplog.text


def test_abgeben_zwei_bilder_zwei_vorbewertungen() -> None:
    lern, client, _, penecho = _flaechen_sitzung("numeric")
    for bild in (PNG, PNG + b"anders"):
        penecho.bild = bild
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1}).status_code == 200
    erste, zweite = (kw["payload"] for kw in _aufrufe(lern, "learning_task_preview"))
    assert erste != zweite
    assert base64.b64decode(zweite["handschrift_png_base64"]) == PNG + b"anders"


def test_passt_nach_neustart_legt_kein_board_an() -> None:
    lern, _, _, _ = _flaechen_sitzung("numeric")
    client2, _, penecho2 = bauen(lern)
    r = client2.post(f"/lernen/api/sitzungen/{SID}/passt", json={"revision": 1, "vorbewertung_id": VOR_ID})
    assert r.status_code == 200, r.text
    # Weder penecho_start_session (sitzung) noch ein anderer PenEcho-Aufruf.
    assert penecho2.aufrufe == []


def test_lern_mcp_nicht_verfuegbar_behaelt_den_code() -> None:
    with pytest.raises(LearningWeg) as weg:
        LernMcp("http://m.test/mcp", ACTOR, transport=_mcp_antwort(
            {"state": "unavailable", "error": {"code": "session_grading_unavailable", "message": "x"}}, fehler=True,
        )).rufe("learning_task_preview", "learning.task.preview")
    assert weg.value.code == "session_grading_unavailable"
    assert LearningWeg("x").code is None


# ---- Bericht am Kursende --------------------------------------------------------

BERICHT = {"attempted": 3, "correct": 2, "score": 0.63, "results": [
    {"ordinal": 1, "item_id": "a", "score": 1.0}, {"ordinal": 2, "item_id": "b", "score": 0.7},
    {"ordinal": 3, "item_id": "c", "score": 0.2}]}
ENDE_MIT_BERICHT = {**ENDE, "summary": BERICHT}


def _berichte(penecho: FakePenEcho) -> list[tuple[Any, ...]]:
    return [a for a in penecho.aufrufe if a[0] == "bericht_zeigen"]


def test_kursende_zeigt_bericht_einmal() -> None:
    lern = FakeLern({
        "learning_course_overview": _ueberblick(turn()),
        "learning_task_next": [{"session": turn()}],
    })
    client, _, penecho = bauen(lern)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    lern.antworten["learning_task_next"] = [{"session": ENDE_MIT_BERICHT}]
    assert client.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 200
    assert len(_berichte(penecho)) == 1
    _, board, sid, text, nummern = _berichte(penecho)[0]
    assert board == "pe-1" and sid == SID and nummern == (1, 2, 3)
    assert "# Bericht: Testkurs" in text and "1 von 3 richtig" in text and "Aufgabe 2: teilweise" in text
    assert "Anl" not in text and "Versuch" not in text
    # Zweites Oeffnen des beendeten Kurses: Kursende-Seite wie bisher, keine zweite Karte.
    lern.antworten["learning_course_overview"] = _ueberblick(None)
    body = client.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()
    assert body["abgeschlossen"] is True and body["turn"]["state"] == "completed"
    assert len(_berichte(penecho)) == 1


def test_kursende_bericht_bei_erstem_oeffnen_nach_neustart(tmp_path) -> None:
    datei = tmp_path / "kursende.json"
    datei.write_text(json.dumps({f"{ACTOR}|{KURS}": SID}), encoding="utf-8")
    lern = FakeLern({"learning_course_overview": _ueberblick(None),
                     "learning_task_next": {"session": ENDE_MIT_BERICHT}})
    client, _, penecho = bauen(lern, kursende=datei)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()["abgeschlossen"] is True
    assert len(_berichte(penecho)) == 1 and _berichte(penecho)[0][2] == SID


def test_bericht_nach_passt() -> None:
    fertig = {**turn(None, completed=5, revision=9, state="completed", mode="training"), "summary": BERICHT}
    _, client, _, penecho = _flaechen_sitzung("numeric", confirm={"session": fertig})
    r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"revision": 1, "vorbewertung_id": VOR_ID})
    assert r.status_code == 200 and r.json()["nummer"] is None
    assert len(_berichte(penecho)) == 1


def test_bericht_fehler_blockiert_kursende_nicht() -> None:
    lern = FakeLern({
        "learning_course_overview": _ueberblick(turn()),
        "learning_task_next": [{"session": turn()}],
    })
    client, _, penecho = bauen(lern, FakePenEcho(fehler=PenEchoNichtErreichbar("x")))
    client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    lern.antworten["learning_task_next"] = [{"session": ENDE_MIT_BERICHT}]
    r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
    assert r.status_code == 200 and r.json()["turn"]["state"] == "completed"


def test_ende_ohne_summary_zeigt_keinen_bericht() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(turn()), "learning_task_next": [{"session": turn()}]})
    client, _, penecho = bauen(lern)
    client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    lern.antworten["learning_task_next"] = [{"session": ENDE}]
    assert client.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 200
    assert _berichte(penecho) == []


def test_abgeben_karte_scheitert_urteil_kommt_als_text_zurueck() -> None:
    # Abnahme 06.11.: PenEcho fand keinen Platz fuer die Karte ("No clear space"), die Leiste blieb stumm.
    lern, client, _, penecho = _flaechen_sitzung("numeric")

    def scheitert(*_a: Any, **_k: Any) -> None:
        raise PenEchoNichtErreichbar("kein Platz")

    penecho.rueckmeldung_zeigen = scheitert  # type: ignore[method-assign]
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    body = r.json()
    assert r.status_code == 200 and body["flaeche"] is False and body["urteil"] == "Richtig."
    assert body["karte"].startswith("Gelesen: -1,5") and "Richtig." in body["karte"]
    assert "$" not in body["karte"] and "\n" not in body["karte"]


def test_abgeben_mit_karte_auf_der_flaeche_ohne_kartentext() -> None:
    _, client, _, _ = _flaechen_sitzung("numeric")
    body = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1}).json()
    assert body["flaeche"] is True and "karte" not in body
