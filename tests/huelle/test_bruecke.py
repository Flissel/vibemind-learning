"""Bruecke der Lern-Huelle: Routen unter /lernen/api/ mit Fakes fuer Learning und PenEcho."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from spaces.learning.huelle.bruecke import (
    FLAECHE_WEG, KEIN_KURS, NICHT_ANGENOMMEN, NICHT_ERREICHBAR, SCHON_ABGEGEBEN, Bruecke,
)
from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.lern_mcp import LearningAbgelehnt, LearningWeg, LernMcp, idempotenz_schluessel
from spaces.learning.huelle.penecho_mcp import PenEchoNichtErreichbar, PenEchoSitzungUnbekannt
from spaces.learning.huelle.sperre import Sperre

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


def turn(task_id: str | None = TASK_1, *, completed: int = 0, revision: int = 1, state: str = "active",
         typ: str = "single_choice", feedback: dict[str, Any] | None = None, course_id: str = KURS) -> dict[str, Any]:
    task = None if task_id is None else {
        "id": task_id, "type": typ, "prompt": f"Frage {task_id[-1]}", "options": [], "difficulty": 0.5,
        "representation": "text", "source_refs": [], "presentation": {},
    }
    return {
        "session_id": SID, "course_id": course_id, "session_revision": revision, "state": state,
        "mode": "training", "task": task, "progress": {"completed": completed, "total": 5},
        "feedback": feedback,
    }


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

    def _pruefen(self, name: str) -> None:
        if self.unbekannt_einmal:
            self.unbekannt_einmal = False
            raise PenEchoSitzungUnbekannt("session_expired")
        if self.fehler is not None:
            raise self.fehler

    def sitzung(self, titel: str) -> str:
        self.aufrufe.append(("sitzung", titel))
        return "pe-1"

    def vergessen(self, titel: str) -> None:
        self.aufrufe.append(("vergessen", titel))

    def aufgabe_setzen(self, session_id: str, aufgabe_id: str, text: str, nummer: int) -> None:
        self.aufrufe.append(("aufgabe_setzen", session_id, aufgabe_id, text, nummer))
        self._pruefen("aufgabe_setzen")

    def notizkarte(self, session_id: str, aufgabe_id: str, titel: str, text: str) -> None:
        self.aufrufe.append(("notizkarte", session_id, aufgabe_id, titel, text))
        self._pruefen("notizkarte")

    def schnappschuss(self, session_id: str, nummer: int) -> bytes:
        self.aufrufe.append(("schnappschuss", session_id, nummer))
        self._pruefen("schnappschuss")
        return self.bild

    def namen(self) -> list[str]:
        return [a[0] for a in self.aufrufe]


def _ueberblick(session: dict[str, Any] | None, revision: int | None = 3) -> dict[str, Any]:
    return {"course_id": KURS, "course_revision": revision,
            "begriffe": [{"concept_key": "k1", "titel": "Begriff", "beherrschung": None}], "session": session}


def _api_leer(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


def bauen(lern: FakeLern, penecho: FakePenEcho | None = None, *, api=_api_leer,
          sperre: Sperre | None = None, einstellung=E) -> tuple[TestClient, Sperre, FakePenEcho]:
    sperre = sperre or Sperre()
    penecho = penecho or FakePenEcho()
    http = httpx.Client(transport=httpx.MockTransport(api))
    bruecke = Bruecke(einstellung, lern, penecho, sperre, http)  # type: ignore[arg-type]
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
    assert ("sitzung", "Testkurs") in penecho.aufrufe


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


def test_antwort_gibt_frei_und_setzt_notizkarte() -> None:
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
    tool, kw = lern.aufrufe[-1]
    assert tool == "learning_task_answer"
    assert kw["expected_revision"] == 1 and kw["payload"] == {"answer": {"choice_id": "a"}} and kw["schreibend"]
    karte = [a for a in penecho.aufrufe if a[0] == "notizkarte"]
    assert len(karte) == 1
    _, _, aufgabe_id, titel, text = karte[0]
    assert aufgabe_id == TASK_1 and titel == "Rückmeldung zu Aufgabe 1"
    assert "Noch nicht richtig." in text and "Begriff: Der Begriff fehlt." in text
    assert body["rueckmeldung"] == text and body["flaeche"] is True
    assert body["turn"]["task"]["id"] == TASK_2 and body["nummer"] == 2
    # Die naechste Aufgabe steht noch nicht auf der Flaeche.
    assert not any(a[0] == "aufgabe_setzen" and a[2] == TASK_2 for a in penecho.aufrufe)


def test_antwort_notizkarte_fehler_wird_geschluckt() -> None:
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

def test_erklaeren_und_hinweis() -> None:
    lern, client, sperre, _ = _offene_sitzung({"session": turn()})
    lern.antworten["learning_tutor_ask"] = {"tutor": {"answer": "So geht es.", "next_step": "Weiter so.",
                                                      "source_refs": [], "evidence_ref": "e"}}
    lern.antworten["learning_hint_request"] = {"session": turn(), "hint": "Denken Sie an X."}
    r = client.post(f"/lernen/api/sitzungen/{SID}/erklaeren", json={})
    assert r.json() == {"answer": "So geht es.", "next_step": "Weiter so."}
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
