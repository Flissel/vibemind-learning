"""Bruecke der Lern-Huelle: Routen unter /lernen/api/ mit Fakes fuer Learning und PenEcho."""

from __future__ import annotations

import base64
import copy
import dataclasses
import io
import json
import re
import threading
import time
from collections.abc import Iterable
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from spaces.learning.huelle.bruecke import (
    BEWERTUNG_WEG, FLAECHE_WEG, KEIN_KURS, KEINE_FLAECHENAUFGABE, KEINE_KORREKTUR, NICHT_ANGENOMMEN,
    NICHT_BESTAETIGT, NICHT_ERREICHBAR, NUR_UEBEN, SCHON_ABGEGEBEN, WIRD_GEPRUEFT, _BEWERTUNG_WEG_CODE, Bruecke,
    urteil_aus,
)
from spaces.learning.huelle.anordnung import anordnung
from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.katalog import KEIN_ZUGANG_MEHR, UEBERSICHT_WEG, Katalog
from spaces.learning.huelle.lern_mcp import LearningAbgelehnt, LearningWeg, LernMcp, idempotenz_schluessel
from spaces.learning.huelle.penecho_mcp import (
    URTEILE, Aufgabenbild, PenEchoBeschaeftigt, PenEchoMcp, PenEchoNichtErreichbar, PenEchoNichtSichtbar,
    PenEchoSitzungUnbekannt, bereich,
)
from spaces.learning.huelle.person import PERSON_COOKIE, person_cookie
from spaces.learning.huelle.sperre import Sperre
from spaces.learning.huelle.tafel import aufgabentext
from spaces.learning.huelle.weiche import Weiche
from spaces.learning.huelle.zugang import COOKIE, token_erzeugen

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
        self.akteure: list[str] = []  # Kennung je Aufruf; die Bruecke muss sie immer nennen

    def rufe(self, tool: str, event_type: str, *, actor_id: str, **kw: Any) -> dict[str, Any]:
        self.akteure.append(actor_id)
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
        # (nummer, rechteck) je aufgabe_setzen; rechteck None = fester Bereich wie bisher.
        self.rechtecke: list[tuple[int, dict[str, int] | None]] = []
        self.karten_rechtecke: list[tuple[int, dict[str, int] | None]] = []
        self.zeigen: list[tuple[int, bool]] = []  # (nummer, zeigen) je aufgabe_setzen
        self.aufnahme_rechtecke: list[tuple[int, dict[str, int] | None]] = []  # je Schnappschuss
        self.aufnahme_timeouts: list[float | None] = []
        self.aufnahme_fehler: Any = None  # rechteck -> Exception | None
        self.bericht_rechtecke: list[dict[int, dict[str, int]] | None] = []
        self.bericht_bilder: list[dict[int, bytes] | None] = []  # je bericht_zeigen; None = Schnappschuesse
        self.rueckmeldung_rechtecke: list[tuple[int, dict[str, int] | None]] = []  # je rueckmeldung_setzen
        # Aufgaben-Canvas: Antworten der neuen Methoden (Standard: Canvas schliesst, kein Dokument).
        self.schliessen_ergebnis = True
        self.dokumente_je_schluessel: dict[str, str] = {}

    def _pruefen(self, name: str) -> None:
        if self.unbekannt_einmal:
            self.unbekannt_einmal = False
            raise PenEchoSitzungUnbekannt("session_expired")
        if self.fehler is not None:
            raise self.fehler

    def sitzung(self, schluessel: str, titel: str, canvas_id: str | None = None, gruppe: str | None = None) -> str:
        if gruppe is not None:  # Teil C: nur die Gruppensitzung nennt ihre Gruppe
            self.aufrufe.append(("sitzung", schluessel, titel, canvas_id, gruppe))
        else:
            self.aufrufe.append(("sitzung", schluessel, titel) if canvas_id is None
                                else ("sitzung", schluessel, titel, canvas_id))
        return "pe-1"

    def vergessen(self, schluessel: str) -> None:
        self.aufrufe.append(("vergessen", schluessel))

    def aufgabe_setzen(self, session_id: str, aufgabe_id: str, text: str, nummer: int, *,
                       mit_ergebnisfeld: bool = False, mit_arbeitsfeld: bool = False,
                       bilder: Any = (), rechteck: dict[str, int] | None = None, zeigen: bool = True) -> None:
        self.aufrufe.append(("aufgabe_setzen", session_id, aufgabe_id, text, nummer))
        self.zeigen.append((nummer, zeigen))
        self.bilder.append(list(bilder))
        self.ergebnisfelder.append((aufgabe_id, nummer, mit_ergebnisfeld))
        self.arbeitsfelder.append((aufgabe_id, nummer, mit_arbeitsfeld))
        self.rechtecke.append((nummer, rechteck))
        if rechteck is None:
            bereich(nummer)  # wie PenEchoMcp: ausserhalb der 20 Bereiche ValueError
        self._pruefen("aufgabe_setzen")

    def bereiche_melden(self, session_id: str, bereiche: Any) -> None:
        self.aufrufe.append(("bereiche_melden", session_id, list(bereiche)))
        self._pruefen("bereiche_melden")

    def bereich_zeigen(self, session_id: str, rechteck: dict[str, int]) -> None:
        self.aufrufe.append(("bereich_zeigen", session_id, rechteck))
        self._pruefen("bereich_zeigen")

    def ergebnis_schnappschuss(self, session_id: str, nummer: int, *, rechteck: dict[str, int] | None = None) -> bytes:
        self.aufrufe.append(("ergebnis_schnappschuss", session_id, nummer))
        self.aufnahme_rechtecke.append((nummer, rechteck))
        self._pruefen("ergebnis_schnappschuss")
        return self.bild

    def rueckmeldung_zeigen(self, session_id: str, aufgabe_id: str, nummer: int, *,
                            gelesen: str | None, urteil: str, text: str,
                            rechteck: dict[str, int] | None = None) -> None:
        self.aufrufe.append(("rueckmeldung_zeigen", session_id, aufgabe_id, nummer, gelesen, urteil, text))
        self.karten_rechtecke.append((nummer, rechteck))
        if rechteck is None:
            bereich(nummer)
        if urteil not in URTEILE:  # wie PenEchoMcp
            raise ValueError("unbekanntes Urteil")
        self._pruefen("rueckmeldung_zeigen")

    def dokument_merken(self, session_id: str) -> bool:
        self.aufrufe.append(("dokument_merken", session_id))
        if self.merken_fehler is not None:
            raise self.merken_fehler
        return True

    def rueckmeldung_setzen(self, session_id: str, aufgabe_id: str, nummer: int, titel: str, text: str, *,
                            rechteck: dict[str, int] | None = None) -> None:
        self.aufrufe.append(("rueckmeldung_setzen", session_id, aufgabe_id, nummer, titel, text))
        self.rueckmeldung_rechtecke.append((nummer, rechteck))
        if rechteck is None:
            bereich(nummer)
        self._pruefen("rueckmeldung_setzen")

    def schnappschuss(self, session_id: str, nummer: int, *, rechteck: dict[str, int] | None = None,
                      timeout: float | None = None) -> bytes:
        self.aufrufe.append(("schnappschuss", session_id, nummer))
        self.aufnahme_rechtecke.append((nummer, rechteck))
        self.aufnahme_timeouts.append(timeout)
        if self.aufnahme_fehler is not None:
            fehler = self.aufnahme_fehler(rechteck)
            if fehler is not None:
                raise fehler
        self._pruefen("schnappschuss")
        return self.bild

    def bericht_zeigen(self, session_id: str, sid: str, text: str, nummern: tuple[int, ...] = (), *,
                       rechteck: Any = None, bilder: Any = None) -> None:
        self.aufrufe.append(("bericht_zeigen", session_id, sid, text, tuple(nummern)))
        self.bericht_rechtecke.append(None if rechteck is None else dict(rechteck))
        self.bericht_bilder.append(None if bilder is None else dict(bilder))
        self._pruefen("bericht_zeigen")

    def schild_setzen(self, session_id: str, request_id: str, text: str, rechteck: dict[str, int]) -> None:
        self.aufrufe.append(("schild_setzen", session_id, request_id, text, dict(rechteck)))
        self._pruefen("schild_setzen")

    # ---- Aufgaben-Canvas (je Aufgabe ein eigenes Dokument, Schluessel `<sid>#<nummer>`) ----
    def aufgabe_aufnehmen(self, session_id: str, rechteck: dict[str, int], *, ergebnis: bool,
                          warten_s: float = 10.0) -> bytes:
        self.aufrufe.append(("aufgabe_aufnehmen", session_id, rechteck, ergebnis))
        self.aufnahme_rechtecke.append((1, rechteck))
        if self.aufnahme_fehler is not None:
            fehler = self.aufnahme_fehler(rechteck)
            if fehler is not None:
                raise fehler
        self._pruefen("aufgabe_aufnehmen")
        return self.bild

    def dokumente_schliessen(self, schluessel: Iterable[str], canvas_id: str) -> dict[str, bool]:
        schluessel = tuple(schluessel)
        self.aufrufe.append(("dokumente_schliessen", schluessel, canvas_id))
        self._pruefen("dokumente_schliessen")
        return {s: self.schliessen_ergebnis for s in schluessel}

    def dokument_von(self, schluessel: str) -> str | None:
        self.aufrufe.append(("dokument_von", schluessel))
        return self.dokumente_je_schluessel.get(schluessel)

    def namen(self) -> list[str]:
        return [a[0] for a in self.aufrufe]


def _ueberblick(session: dict[str, Any] | None, revision: int | None = 3) -> dict[str, Any]:
    return {"course_id": KURS, "course_revision": revision,
            "begriffe": [{"concept_key": "k1", "titel": "Begriff", "beherrschung": None}], "session": session}


def _api_leer(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


def bauen(lern: FakeLern, penecho: FakePenEcho | None = None, *, api=_api_leer,
          sperre: Sperre | None = None, einstellung=E, kursende=None, katalog: Katalog | None = None,
          actor: str = ACTOR, bewerter: Any = None) -> tuple[TestClient, Sperre, FakePenEcho]:
    sperre = sperre or Sperre()
    penecho = penecho or FakePenEcho()
    http = httpx.Client(transport=httpx.MockTransport(api))
    bruecke = Bruecke(einstellung, lern, penecho, sperre, http, kursende, katalog=katalog,  # type: ignore[arg-type]
                      bewerter=bewerter)
    return als(TestClient(bruecke), actor), sperre, penecho


def als(client: TestClient, actor: str) -> TestClient:
    """Wie die Weiche: die Kennung der Person steht je Anfrage in `scope["state"]["actor_id"]`."""
    client.app_state["actor_id"] = actor
    return client


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
    # Teil A2: neue Durchlaeufe sind feste Listen; wie sie laufen, sagt danach der Turn (`ablauf`).
    assert start["payload"] == {"course_revision": 3, "mode": "training", "blueprint": {"max_attempts": 20},
                                "ablauf": "liste"}


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


def test_unbekannter_kurs_404_fremder_kurs_403() -> None:
    # Teil B: ein Kurs ausserhalb der eigenen Liste ist kein Zugang (403); keine Kurskennung bleibt 404.
    lern = FakeLern({})
    client, _, _ = bauen(lern)
    kein_zugang = {"fehler": "kein_zugang", "text": KEIN_ZUGANG_MEHR, "meldung": KEIN_ZUGANG_MEHR}
    for r in (client.post(f"/lernen/api/kurse/{FREMD}/oeffnen"), client.post(f"/lernen/api/kurse/{FREMD}/neu"),
              client.get(f"/lernen/api/kurse/{FREMD}/bewertung"), client.get(f"/lernen/api/notizen?course_id={FREMD}")):
        assert r.status_code == 403 and r.json() == kein_zugang
    assert client.post("/lernen/api/kurse/kein-uuid/oeffnen").status_code == 404
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

def test_actor_kommt_nur_aus_der_anfrage_der_weiche() -> None:
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

    # Ohne eigene Kennung im Client: jede Anfrage muss sie nennen (die Weiche setzt sie).
    lern = LernMcp("http://learning-mcp.test/mcp", transport=httpx.MockTransport(mcp))
    http = httpx.Client(transport=httpx.MockTransport(api))
    client = als(TestClient(Bruecke(E, lern, FakePenEcho(), Sperre(), http)), ACTOR)  # type: ignore[arg-type]
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


# ---- Teil A2, Task 4: Listenkurs beim Oeffnen ganz auf der Flaeche ------------------------------

ZU_VIELE = "Dieser Kurs hat mehr Aufgaben, als auf eine Fläche passen."


def _listen_task(n: int, *, typ: str = "numeric", bild: str | None = None) -> dict[str, Any]:
    presentation: dict[str, Any] = {} if bild is None else {"image": f"/lernsequenz-medien/{bild}"}
    return {"id": f"44444444-4444-4444-8444-4444444445{n:02d}", "type": typ, "prompt": f"Frage {n}", "options": [],
            "difficulty": 0.5, "representation": "text", "source_refs": [], "presentation": presentation}


def _eintrag(n: int, status: str = "offen", *, vor: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return {"attempt_id": f"77777777-7777-4777-8777-7777777777{n:02d}", "nummer": n, "task": _listen_task(n, **kw),
            "status": status, "vorbewertung": vor}


def _listen_turn(eintraege: list[dict[str, Any]]) -> dict[str, Any]:
    erste = next(e for e in eintraege if e["status"] == "offen")
    t = turn(erste["task"]["id"], completed=sum(e["status"] == "bestaetigt" for e in eintraege), mode="training",
             typ=erste["task"]["type"])
    t["ablauf"] = "liste"
    t["progress"]["total"] = len(eintraege)
    return t


def _listen_lern(eintraege: list[dict[str, Any]], *, laufend: bool = True) -> FakeLern:
    t = _listen_turn(eintraege)
    antworten: dict[str, Any] = {
        "learning_course_overview": _ueberblick(t if laufend else None),
        "learning_task_next": {"session": t},
        "learning_task_list": {"session": t, "aufgaben": eintraege},
    }
    if not laufend:
        antworten["learning_session_start"] = {"session": t}
    return FakeLern(antworten)


class HaltePenEcho(FakePenEcho):
    """Haelt das Setzen im Hintergrund an, bis `frei` gesetzt ist (nur Aufgaben mit Rechteck ausser `sofort`)."""

    def __init__(self, sofort: int) -> None:
        super().__init__()
        self.sofort = sofort
        self.frei = threading.Event()

    def aufgabe_setzen(self, session_id: str, aufgabe_id: str, text: str, nummer: int, **kw: Any) -> None:
        if kw.get("rechteck") is not None and nummer != self.sofort:
            assert self.frei.wait(5)
        super().aufgabe_setzen(session_id, aufgabe_id, text, nummer, **kw)


def _bis(bedingung: Any, zeit: float = 5.0) -> None:
    ende = time.monotonic() + zeit
    while not bedingung():
        assert time.monotonic() < ende, "Spur wurde nicht fertig"
        time.sleep(0.01)


def test_oeffnen_liste_setzt_erste_und_reiht_rest_ein() -> None:
    eintraege = [_eintrag(1, "bestaetigt"), _eintrag(2), _eintrag(3), _eintrag(4)]
    lern = _listen_lern(eintraege, laufend=False)
    penecho = HaltePenEcho(sofort=2)
    client, _, _ = bauen(lern, penecho)
    bruecke = client.app
    try:
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["nummer"] == 2 and body["flaeche"] is True and body["turn"]["ablauf"] == "liste"
        assert _aufrufe(lern, "learning_session_start")[0]["payload"]["ablauf"] == "liste"
        (liste,) = _aufrufe(lern, "learning_task_list")
        assert liste["session_id"] == SID
        bereiche = anordnung([False] * 4)
        assert ("bereiche_melden", "pe-1", bereiche) in penecho.aufrufe  # 4 Bereiche gemeldet
        assert penecho.rechtecke[0] == (2, bereiche[1])
        assert ("aufgabe_setzen", "pe-1", eintraege[1]["task"]["id"], "Frage 2", 2) in penecho.aufrufe
        namen = penecho.namen()
        assert namen.index("bereiche_melden") < namen.index("aufgabe_setzen") < namen.index("bereich_zeigen")
        assert ("bereich_zeigen", "pe-1", bereiche[1]) in penecho.aufrufe
        spur = bruecke._flaechenspur
        assert spur.wartend(f"{SID}:setzen:") == 3
        assert all(spur.zustand(f"{SID}:setzen:{n}") in ("wartet", "laeuft") for n in (1, 3, 4))
        assert spur.zustand(f"{SID}:setzen:2") is None
        penecho.frei.set()
        _bis(lambda: spur.wartend(f"{SID}:setzen:") == 0)
        assert sorted(penecho.rechtecke[1:], key=lambda t: t[0]) == [(1, bereiche[0]), (3, bereiche[2]), (4, bereiche[3])]
        # Im Hintergrund wird nichts gezeigt: die Ansicht bleibt bei Aufgabe 2.
        assert [a for a in penecho.aufrufe if a[0] == "bereich_zeigen"] == [("bereich_zeigen", "pe-1", bereiche[1])]
    finally:
        penecho.frei.set()
        bruecke.beenden()


def test_oeffnen_zeigt_vorhandene_rueckmeldung() -> None:
    vor = _vor(score=1.0, gelesen="4")
    eintraege = [_eintrag(1, "bestaetigt", vor=_vor(vid=VOR_ID_2)), _eintrag(2, vor=vor), _eintrag(3)]
    lern = _listen_lern(eintraege)
    client, _, penecho = bauen(lern)
    bruecke = client.app
    try:
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
        assert r.status_code == 200, r.text
        bereiche = anordnung([False] * 3)
        (karte,) = _karten(penecho)
        assert karte == ("rueckmeldung_zeigen", "pe-1", eintraege[1]["task"]["id"], 2, "4", "Richtig.",
                         "Gut gemacht.")
        assert penecho.karten_rechtecke == [(2, bereiche[1])]
        namen = penecho.namen()
        assert namen.index("aufgabe_setzen") < namen.index("rueckmeldung_zeigen") < namen.index("bereich_zeigen")
        _bis(lambda: bruecke._flaechenspur.wartend(f"{SID}:setzen:") == 0)
        # Die bestaetigte Aufgabe 1 kommt ohne Karte auf die Flaeche.
        assert len(_karten(penecho)) == 1
        assert sorted(n for n, _ in penecho.rechtecke) == [1, 2, 3]
    finally:
        bruecke.beenden()


def test_oeffnen_liste_karte_einer_spaeteren_aufgabe_im_hintergrund() -> None:
    eintraege = [_eintrag(1), _eintrag(2, vor=_vor(score=0.0, gelesen="7"))]
    client, _, penecho = bauen(_listen_lern(eintraege))
    bruecke = client.app
    try:
        assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
        _bis(lambda: bruecke._flaechenspur.wartend(f"{SID}:setzen:") == 0)
        (karte,) = _karten(penecho)
        assert karte[3:6] == (2, "7", "Noch nicht richtig.")
        assert penecho.karten_rechtecke == [(2, anordnung([False] * 2)[1])]
    finally:
        bruecke.beenden()


def test_oeffnen_liste_grosse_bereiche_fuer_bilder_und_zeichnungen() -> None:
    eintraege = [_eintrag(1, bild="graph-1.png"), _eintrag(2, typ="penecho_canvas"), _eintrag(3)]
    client, _, penecho = bauen(_listen_lern(eintraege))
    try:
        assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
        (gemeldet,) = [a for a in penecho.aufrufe if a[0] == "bereiche_melden"]
        assert gemeldet[2] == anordnung([True, True, False])
    finally:
        client.app.beenden()


def test_oeffnen_liste_hintergrundfehler_bricht_nichts_ab() -> None:
    eintraege = [_eintrag(1), _eintrag(2), _eintrag(3)]
    penecho = FakePenEcho()
    client, _, penecho = bauen(_listen_lern(eintraege), penecho)
    bruecke = client.app
    echt = penecho.aufgabe_setzen

    def setzen(session_id: str, aufgabe_id: str, text: str, nummer: int, **kw: Any) -> None:
        echt(session_id, aufgabe_id, text, nummer, **kw)
        if nummer == 2:
            raise RuntimeError("kaputt")  # kein PenEcho-Fehler: nur die Spur faengt ihn

    penecho.aufgabe_setzen = setzen  # type: ignore[method-assign]
    try:
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
        assert r.status_code == 200 and r.json()["nummer"] == 1
        _bis(lambda: bruecke._flaechenspur.wartend(f"{SID}:setzen:") == 0)
        assert sorted(n for n, _ in penecho.rechtecke) == [1, 2, 3]
    finally:
        bruecke.beenden()


def test_alte_sitzung_bleibt_anpassend() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(turn()), "learning_task_next": {"session": turn()}})
    client, _, penecho = bauen(lern)
    try:
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
        assert r.status_code == 200 and r.json()["nummer"] == 1
        assert "learning_task_list" not in lern.tools()
        assert "bereiche_melden" not in penecho.namen() and "bereich_zeigen" not in penecho.namen()
        assert penecho.rechtecke == [(1, None)]
        assert client.app._flaechenspur.wartend("") == 0
    finally:
        client.app.beenden()


def test_zu_viele_aufgaben_meldung() -> None:
    eintraege = [_eintrag(n) for n in range(1, 44)]
    client, _, penecho = bauen(_listen_lern(eintraege))
    try:
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
        assert r.status_code == 409
        assert r.json() == {"meldung": ZU_VIELE, "code": "zu_viele_aufgaben"}
        assert "aufgabe_setzen" not in penecho.namen() and "bereiche_melden" not in penecho.namen()
    finally:
        client.app.beenden()


def test_beenden_stoppt_die_flaechenspur() -> None:
    client, _, _ = bauen(FakeLern({}))
    client.app.beenden()
    assert client.app._flaechenspur.einreihen("x", lambda: None) is False


# ---- Review 4dec9b7: alte Hintergrundarbeiten holen kein Board nach vorn -----------------------

class MerkSpur:
    """Statt der Flaechenspur: merkt die Arbeiten und fuehrt sie erst auf Zuruf aus, unter der Sperre wie Spur."""

    def __init__(self) -> None:
        self.arbeiten: list[tuple[str, Any]] = []
        self.vorrang: dict[str, int] = {}
        self.wer: dict[str, str | None] = {}

    def einreihen(self, schluessel: str, arbeit: Any, *, vorrang: int = 1, wer: str | None = None) -> bool:
        self.arbeiten.append((schluessel, arbeit))
        self.vorrang[schluessel] = vorrang
        self.wer[schluessel] = wer
        return True

    def wartend(self, praefix: str) -> int:
        return sum(1 for s, _ in self.arbeiten if s.startswith(praefix))

    def zustand(self, schluessel: str) -> str | None:
        return "wartet" if any(s == schluessel for s, _ in self.arbeiten) else None

    def abarbeiten(self, sperre: Any) -> None:
        # Wie Spur: kleiner Vorrang zuerst, sonst in der Reihenfolge des Einreihens.
        arbeiten = sorted(self.arbeiten, key=lambda a: self.vorrang[a[0]])
        self.arbeiten = []
        for _, arbeit in arbeiten:
            with sperre:
                arbeit()

    def beenden(self, timeout: float = 5.0) -> None:
        self.arbeiten = []


def _mit_merkspur(client: TestClient) -> MerkSpur:
    bruecke = client.app
    bruecke._flaechenspur.beenden()  # die echte Flaechenspur anhalten; die Modellspur bleibt
    merk = MerkSpur()
    bruecke._spuren = [merk if s is bruecke._flaechenspur else s for s in bruecke._spuren]
    bruecke._flaechenspur = merk
    return merk


def test_alte_hintergrundarbeiten_nach_kurswechsel_fallen_weg() -> None:
    eintraege = [_eintrag(1), _eintrag(2), _eintrag(3)]
    liste_a = _listen_turn(eintraege)
    b = turn_b(mode="training")
    lern = FakeLern({
        "learning_course_overview": lambda kw: _ueberblick(liste_a if kw["course_id"] == KURS else b),
        "learning_task_next": lambda kw: {"session": liste_a if kw["session_id"] == SID else b},
        "learning_task_list": {"session": liste_a, "aufgaben": eintraege},
    })
    client, _, penecho = bauen(lern, einstellung=E2)
    merk = _mit_merkspur(client)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    assert [s for s, _ in merk.arbeiten] == [f"{SID}:setzen:2", f"{SID}:setzen:3"]
    assert client.post(f"/lernen/api/kurse/{KURS_B}/oeffnen").status_code == 200
    vorher = len(penecho.aufrufe)
    merk.abarbeiten(client.app._flaeche_sperre)
    # Keine Arbeit fuer Kurs A mehr: weder setzen noch zeigen, auch keine PenEcho-Sitzung fuer A.
    assert penecho.aufrufe[vorher:] == []


def test_hintergrundarbeiten_zeigen_nichts() -> None:
    eintraege = [_eintrag(1), _eintrag(2, vor=_vor()), _eintrag(3)]
    client, _, penecho = bauen(_listen_lern(eintraege))
    merk = _mit_merkspur(client)
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
    # Gerechte KI (Task 9): jede Arbeit wird mit der echten Person eingereiht.
    assert merk.wer and set(merk.wer.values()) == {ACTOR}
    vorher = len(penecho.aufrufe)
    merk.abarbeiten(client.app._flaeche_sperre)
    neu = [a[0] for a in penecho.aufrufe[vorher:]]
    assert neu.count("aufgabe_setzen") == 2 and "rueckmeldung_zeigen" in neu and "bereich_zeigen" not in neu
    # Erste Aufgabe darf das Board sichtbar machen; die im Hintergrund gesetzten nicht.
    assert penecho.zeigen == [(1, True), (2, False), (3, False)]


# ---- Teil A2, Task 5: Abgaben im Hintergrund und Aufgabenwahl --------------------------------------

LAEUFT_SCHON = "Diese Aufgabe wird gerade bewertet."
SCHON_BESTAETIGT = "Diese Aufgabe ist schon bestätigt."


def _vid(n: int) -> str:
    return f"55555555-5555-4555-8555-5555555556{n:02d}"


class ListenLern:
    """Learning eines Listenkurses mit Zustand: Vorbewerten speichert die Vorbewertung, Passt bestaetigt.

    `halten(n)` haelt die Vorbewertung von Aufgabe n an einem Event an (wie ein langsames Modell);
    `fehler[n]` laesst sie scheitern.
    """

    def __init__(self, n: int, *, typ: str = "numeric") -> None:
        self.eintraege = [_eintrag(i, typ=typ) for i in range(1, n + 1)]
        self.halt: dict[int, threading.Event] = {}
        self.fehler: dict[int, Exception] = {}
        self.lern = FakeLern({
            "learning_course_overview": lambda kw: _ueberblick(self.turn()),
            "learning_task_next": lambda kw: {"session": self.turn()},
            "learning_task_list": lambda kw: {"session": self.turn(), "aufgaben": copy.deepcopy(self.eintraege)},
            "learning_task_preview": self._vorbewerten,
            "learning_task_confirm": self._bestaetigen,
            "learning_task_answer": self._beantworten,
        })

    def turn(self) -> dict[str, Any]:
        if any(e["status"] == "offen" for e in self.eintraege):
            return _listen_turn(self.eintraege)
        fertig = turn(None, completed=len(self.eintraege), revision=9, state="completed", mode="training")
        return {**fertig, "ablauf": "liste", "summary": BERICHT}

    def halten(self, n: int) -> threading.Event:
        self.halt[n] = threading.Event()
        return self.halt[n]

    def _nummer(self, kw: dict[str, Any]) -> int:
        aid = kw["payload"]["attempt_id"]
        (e,) = [e for e in self.eintraege if e["attempt_id"] == aid]
        return e["nummer"]

    def _vorbewerten(self, kw: dict[str, Any]) -> dict[str, Any]:
        n = self._nummer(kw)
        if n in self.halt:
            assert self.halt[n].wait(2), "Vorbewertung nie freigegeben"
        if n in self.fehler:
            raise self.fehler[n]
        vor = _vor(vid=_vid(n), gelesen=str(n))
        self.eintraege[n - 1]["vorbewertung"] = vor
        return {"session": self.turn(), "vorbewertung": vor}

    def _bestaetigen(self, kw: dict[str, Any]) -> dict[str, Any]:
        n = self._nummer(kw)
        self.eintraege[n - 1]["status"] = "bestaetigt"
        return {"session": self.turn()}

    def _beantworten(self, kw: dict[str, Any]) -> dict[str, Any]:
        if "attempt_id" not in kw["payload"]:
            raise LearningAbgelehnt("list sessions need attempt_id", "invalid_session_request")
        n = self._nummer(kw)
        self.eintraege[n - 1]["status"] = "bestaetigt"
        t = self.turn()
        t["feedback"] = {"state": "correct", "score": 1.0, "rationale_codes": [], "source_refs": []}
        return {"session": t}

    def vorschauen(self) -> list[dict[str, Any]]:
        return _aufrufe(self.lern, "learning_task_preview")


def _ruhig(bruecke: Bruecke) -> bool:
    """Keine Abgabe mehr in der Modellspur und keine Rueckmeldung mehr auf dem Weg zur Flaeche."""
    return bruecke._modellspur.wartend(f"{SID}:abgabe:") == 0 and \
        bruecke._flaechenspur.wartend(f"{SID}:rueckmeldung:") == 0


def _stand(client: TestClient) -> dict[str, Any]:
    r = client.get(f"/lernen/api/sitzungen/{SID}/stand")
    assert r.status_code == 200, r.text
    return r.json()


def _status(stand: dict[str, Any], n: int) -> str:
    return stand["aufgaben"][n - 1]["status"]


def _listen_offen(ll: ListenLern, penecho: FakePenEcho | None = None, **kw: Any):
    client, sperre, penecho = bauen(ll.lern, penecho, **kw)
    r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    assert r.status_code == 200, r.text
    _bis(lambda: client.app._flaechenspur.wartend(f"{SID}:setzen:") == 0, 2)
    return client, penecho, r.json()


def test_abgeben_antwortet_sofort() -> None:
    ll = ListenLern(3)
    halt = ll.halten(2)
    client, penecho, offen = _listen_offen(ll)
    bruecke = client.app
    try:
        assert offen["stand"]["ablauf"] == "liste" and offen["stand"]["aktuell"] == 1
        assert [a["nummer"] for a in offen["aufgaben"]] == [1, 2, 3]
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2})
        assert r.status_code == 202, r.text
        assert {k: v for k, v in r.json().items() if k != "stand"} == {"nummer": 2, "status": "in_bewertung", "wartend": 1}
        stand = _stand(client)
        assert stand["ablauf"] == "liste" and stand["wartend"] == 1 and stand["aktuell"] == 1
        assert [_status(stand, n) for n in (1, 2, 3)] == ["offen", "in_bewertung", "offen"]
        bereiche = anordnung([False] * 3)
        # Das Bild kam synchron aus dem Ergebnisfeld der Aufgabe 2, mit ihrem Rechteck.
        assert ("ergebnis_schnappschuss", "pe-1", 2) in penecho.aufrufe
        assert penecho.aufnahme_rechtecke == [(2, bereiche[1])]
        halt.set()
        _bis(lambda: _ruhig(bruecke), 2)
        stand = _stand(client)
        assert stand["wartend"] == 0
        assert stand["aufgaben"][1] == {"nummer": 2, "status": "rueckmeldung", "urteil": "Richtig.",
                                        "vorbewertung_id": _vid(2), "meldung": None}
        assert (2, bereiche[1]) in penecho.karten_rechtecke
        (karte,) = [k for k in _karten(penecho) if k[3] == 2]
        assert karte[4:6] == ("2", "Richtig.")
        (kw,) = ll.vorschauen()
        assert kw["payload"]["attempt_id"] == ll.eintraege[1]["attempt_id"]
        assert set(kw["payload"]) == {"handschrift_png_base64", "attempt_id"} and kw["schreibend"] is True
    finally:
        halt.set()
        bruecke.beenden()


def test_doppelte_abgabe() -> None:
    ll = ListenLern(2)
    halt = ll.halten(1)
    client, _, _ = _listen_offen(ll)
    bruecke = client.app
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1}).status_code == 202
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1})
        assert r.status_code == 409
        assert r.json()["code"] == "laeuft_schon" and r.json()["meldung"] == LAEUFT_SCHON
        halt.set()
        _bis(lambda: _ruhig(bruecke), 2)
        assert len(ll.vorschauen()) == 1
    finally:
        halt.set()
        bruecke.beenden()


def test_passt_ueberspringt_aufgabe_in_bewertung() -> None:
    # Abnahme A2 (Befund 3): „Passt“ sprang auf eine abgegebene Aufgabe, die noch bewertet wurde.
    ll = ListenLern(4)
    ll.eintraege[2]["vorbewertung"] = _vor(vid=_vid(3), gelesen="3")
    halt = ll.halten(4)
    client, penecho, _ = _listen_offen(ll)
    bruecke = client.app
    bereiche = anordnung([False] * 4)
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 4}).status_code == 202
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": 3, "vorbewertung_id": _vid(3)})
        assert r.status_code == 200, r.text
        assert r.json()["weiter_zu"] == 1
        assert ("bereich_zeigen", "pe-1", bereiche[0]) in penecho.aufrufe[vorher:]
    finally:
        halt.set()
        _bis(lambda: _ruhig(bruecke), 2)
        bruecke.beenden()


def test_naechste_offene_nimmt_bewertete_nur_als_letzte_wahl() -> None:
    from spaces.learning.huelle.bruecke import _naechste_offene

    def liste(*status: str) -> list[dict[str, Any]]:
        return [{"nummer": i, "status": s} for i, s in enumerate(status, start=1)]

    assert _naechste_offene(liste("offen", "bestaetigt", "in_bewertung", "rueckmeldung"), 2) == 4
    assert _naechste_offene(liste("in_bewertung", "bestaetigt", "in_bewertung", "offen"), 2) == 4
    # Nur noch Aufgaben in Bewertung: dann doch die naechste davon (sonst stuende die Ansicht still).
    assert _naechste_offene(liste("in_bewertung", "bestaetigt", "in_bewertung"), 2) == 3
    assert _naechste_offene(liste("bestaetigt", "bestaetigt"), 1) is None


def test_passt_waehrend_bewertung() -> None:
    ll = ListenLern(5)
    ll.eintraege[2]["vorbewertung"] = _vor(vid=_vid(3), gelesen="3")
    halt = ll.halten(5)
    client, penecho, _ = _listen_offen(ll)
    bruecke = client.app
    bereiche = anordnung([False] * 5)
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 5}).status_code == 202
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": 3, "vorbewertung_id": _vid(3)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["weiter_zu"] == 4 and body["ablauf"] == "liste"
        assert [a["status"] for a in body["aufgaben"]] == ["offen", "offen", "bestaetigt", "offen", "in_bewertung"]
        assert body["wartend"] == 1
        (kw,) = _aufrufe(ll.lern, "learning_task_confirm")
        assert kw["payload"] == {"vorbewertung_id": _vid(3), "attempt_id": ll.eintraege[2]["attempt_id"]}
        assert ("bereich_zeigen", "pe-1", bereiche[3]) in penecho.aufrufe[vorher:]
        halt.set()
        _bis(lambda: _ruhig(bruecke), 2)
        assert (5, bereiche[4]) in penecho.karten_rechtecke
        assert _status(_stand(client), 5) == "rueckmeldung"
    finally:
        halt.set()
        bruecke.beenden()


def test_neustart_waehrend_bewertung() -> None:
    ll = ListenLern(2)
    halt = ll.halten(1)
    client, _, _ = _listen_offen(ll)
    client2, _, _ = bauen(ll.lern)
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1}).status_code == 202
        # Neue Bruecke: nichts im Speicher, Learning kennt (noch) keine Vorbewertung.
        stand = _stand(client2)
        assert _status(stand, 1) == "offen" and stand["wartend"] == 0
        r = client2.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1})
        assert r.status_code == 202, r.text
    finally:
        halt.set()
        client.app.beenden()
        client2.app.beenden()


def test_bewertung_weg_wird_fehler() -> None:
    ll = ListenLern(2)
    ll.fehler[1] = LearningWeg("x", code=_BEWERTUNG_WEG_CODE)
    client, _, _ = _listen_offen(ll)
    bruecke = client.app
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1}).status_code == 202
        _bis(lambda: _ruhig(bruecke), 2)
        eintrag = _stand(client)["aufgaben"][0]
        assert eintrag["status"] == "fehler" and eintrag["meldung"] == BEWERTUNG_WEG
        del ll.fehler[1]
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1}).status_code == 202
        _bis(lambda: _ruhig(bruecke), 2)
        eintrag = _stand(client)["aufgaben"][0]
        assert eintrag["status"] == "rueckmeldung" and eintrag["meldung"] is None
    finally:
        bruecke.beenden()


def test_andere_fehler_der_bewertung_werden_fehler() -> None:
    ll = ListenLern(3)
    ll.fehler[1] = LearningWeg("x")
    ll.fehler[2] = LearningAbgelehnt("english text", "invalid_session_request")
    client, _, _ = _listen_offen(ll)
    bruecke = client.app
    try:
        for n in (1, 2):
            assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": n}).status_code == 202
        _bis(lambda: _ruhig(bruecke), 2)
        stand = _stand(client)
        assert [(a["status"], a["meldung"]) for a in stand["aufgaben"][:2]] == [
            ("fehler", NICHT_ERREICHBAR), ("fehler", NICHT_ANGENOMMEN)]
    finally:
        bruecke.beenden()


def test_passt_letzte_aufgabe_beendet_kurs(tmp_path) -> None:
    datei = tmp_path / "kursende.json"
    ll = ListenLern(2)
    ll.eintraege[0]["status"] = "bestaetigt"
    ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(2))
    client, penecho, _ = _listen_offen(ll, kursende=datei)
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": 2, "vorbewertung_id": _vid(2)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["weiter_zu"] is None and body["turn"]["state"] == "completed"
        assert [a["status"] for a in body["aufgaben"]] == ["bestaetigt", "bestaetigt"] and body["aktuell"] == 2
        assert json.loads(datei.read_text(encoding="utf-8")) == {f"{ACTOR}|{KURS}": SID}
        (bericht,) = _berichte(penecho)
        bereiche = anordnung([False] * 2)
        assert bericht[4] == (1, 2) and penecho.bericht_rechtecke == [{1: bereiche[0], 2: bereiche[1]}]
    finally:
        client.app.beenden()


def test_abgeben_bestaetigt_und_ungueltig() -> None:
    ll = ListenLern(2, typ="code")
    ll.eintraege[0]["status"] = "bestaetigt"
    client, penecho, _ = _listen_offen(ll)
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1})
        assert r.status_code == 409
        assert r.json()["code"] == "schon_bestaetigt" and r.json()["meldung"] == SCHON_BESTAETIGT
        assert r.json()["stand"]["aufgaben"][0]["status"] == "bestaetigt"
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2})
        assert r.status_code == 400 and r.json()["meldung"] == KEINE_FLAECHENAUFGABE
        for body in ({"nummer": 0}, {"nummer": 3}, {"nummer": True}, {"nummer": "1"}):
            assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json=body).status_code == 400, body
        assert ll.vorschauen() == [] and penecho.aufnahme_rechtecke == []
        # Nach einer Ablehnung ist die Nummer nicht mehr belegt.
        assert _status(_stand(client), 2) == "offen"
    finally:
        client.app.beenden()


def test_abgeben_ohne_flaeche_503_und_frei() -> None:
    ll = ListenLern(2)
    client, penecho, _ = _listen_offen(ll)
    try:
        penecho.fehler = PenEchoNichtErreichbar("weg")
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1})
        assert r.status_code == 503 and r.json() == {"meldung": FLAECHE_WEG}
        assert ll.vorschauen() == [] and _status(_stand(client), 1) == "offen"
    finally:
        client.app.beenden()


def test_abgeben_open_nimmt_den_bereich_mit_rechteck() -> None:
    ll = ListenLern(2, typ="open")
    client, penecho, _ = _listen_offen(ll)
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2}).status_code == 202
        _bis(lambda: _ruhig(client.app), 2)
        assert ("schnappschuss", "pe-1", 2) in penecho.aufrufe
        assert penecho.aufnahme_rechtecke == [(2, anordnung([False] * 2)[1])]
    finally:
        client.app.beenden()


def test_zeigen_holt_die_gewaehlte_aufgabe() -> None:
    ll = ListenLern(3)
    client, penecho, _ = _listen_offen(ll)
    try:
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 3})
        assert r.status_code == 200, r.text
        assert [a for a in penecho.aufrufe[vorher:] if a[0] == "bereich_zeigen"] == [
            ("bereich_zeigen", "pe-1", anordnung([False] * 3)[2])]
        assert client.post(f"/lernen/api/sitzungen/{SID}/zeigen", json={"nummer": 4}).status_code == 400
    finally:
        client.app.beenden()


def test_korrektur_im_listenkurs_mit_attempt_id() -> None:
    ll = ListenLern(2)
    ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(2), gelesen="7")
    client, penecho, _ = _listen_offen(ll)
    try:
        body = {"nummer": 2, "vorbewertung_id": _vid(2), "answer": {"value": 2}}
        r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur", json=body)
        assert r.status_code == 200, r.text
        (kw,) = ll.vorschauen()
        assert kw["payload"] == {"answer": {"value": 2}, "korrigiert_aus": _vid(2),
                                 "attempt_id": ll.eintraege[1]["attempt_id"]}
        assert r.json()["urteil"] == "Richtig." and r.json()["stand"]["aufgaben"][1]["status"] == "rueckmeldung"
        assert penecho.karten_rechtecke[-1] == (2, anordnung([False] * 2)[1])
    finally:
        client.app.beenden()


def test_stand_nur_fuer_listenkurse() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(turn()), "learning_task_next": {"session": turn()}})
    client, _, _ = bauen(lern)
    try:
        assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
        assert client.get(f"/lernen/api/sitzungen/{SID}/stand").status_code == 400
        assert "stand" not in client.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()
    finally:
        client.app.beenden()


def test_alte_setzen_arbeit_ueberschreibt_die_frische_karte_nicht() -> None:
    # Aufgabe 2 hat beim Oeffnen eine alte Vorbewertung; ihre setzen-Arbeit wartet noch, als die neue
    # Abgabe fertig wird. Die Rueckmeldung (vorrang 0) kommt zuerst; die alte Karte darf danach nicht folgen.
    ll = ListenLern(3)
    ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(9), gelesen="alt")
    client, _, penecho = bauen(ll.lern)
    bruecke = client.app
    merk = _mit_merkspur(client)
    try:
        assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2}).status_code == 202
        _bis(lambda: bruecke._modellspur.wartend(f"{SID}:abgabe:") == 0, 2)
        assert [s for s, _ in merk.arbeiten] == [f"{SID}:setzen:2", f"{SID}:setzen:3", f"{SID}:rueckmeldung:2"]
        merk.abarbeiten(bruecke._flaeche_sperre)
        assert [k[4] for k in _karten(penecho) if k[3] == 2] == ["2"]
        assert sorted(n for n, _ in penecho.rechtecke) == [1, 2, 3]  # die Aufgabe selbst kam trotzdem
    finally:
        bruecke.beenden()

# ---- Review 4789823: Stand in 202/409, alte Nummernwege im Listenkurs ----------------------------------

NICHT_IM_LISTENMODUS = "Bitte geben Sie diese Aufgabe über „Abgeben“ ab."


def test_abgeben_202_und_laeuft_schon_tragen_den_stand() -> None:
    ll = ListenLern(2)
    halt = ll.halten(1)
    client, _, _ = _listen_offen(ll)
    bruecke = client.app
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1})
        assert r.status_code == 202
        stand = r.json()["stand"]
        assert stand["ablauf"] == "liste" and stand["wartend"] == 1
        assert [a["status"] for a in stand["aufgaben"]] == ["in_bewertung", "offen"]
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 1})
        assert r.status_code == 409 and r.json()["code"] == "laeuft_schon"
        assert [a["status"] for a in r.json()["stand"]["aufgaben"]] == ["in_bewertung", "offen"]
    finally:
        halt.set()
        bruecke.beenden()


def test_antwort_im_listenkurs_mit_attempt_id_und_rechteck() -> None:
    ll = ListenLern(3)
    ll.eintraege[1]["task"]["type"] = "code"
    client, penecho, _ = _listen_offen(ll)
    bereiche = anordnung([False] * 3)
    try:
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"nummer": 2, "answer": {"text": "x"}})
        assert r.status_code == 200, r.text
        (kw,) = _aufrufe(ll.lern, "learning_task_answer")
        assert kw["payload"] == {"answer": {"text": "x"}, "attempt_id": ll.eintraege[1]["attempt_id"]}
        assert kw["schreibend"] is True
        body = r.json()
        assert body["ablauf"] == "liste" and body["weiter_zu"] == 3
        assert [a["status"] for a in body["aufgaben"]] == ["offen", "bestaetigt", "offen"]
        assert body["rueckmeldung"] == "Richtig – gut gemacht." and body["flaeche"] is True
        # Rueckmeldung am Rechteck der Liste, kein Setzen am alten Raster, kein automatisches Weiter.
        assert penecho.rueckmeldung_rechtecke == [(2, bereiche[1])]
        neu = [a[0] for a in penecho.aufrufe[vorher:]]
        assert "aufgabe_setzen" not in neu
        assert "learning_task_next" not in [t for t, _ in ll.lern.aufrufe[-3:]]
    finally:
        client.app.beenden()


def test_weiter_im_listenkurs_zeigt_aktuell_ohne_altes_raster() -> None:
    ll = ListenLern(3)
    ll.eintraege[2]["vorbewertung"] = _vor(vid=_vid(3))
    client, penecho, _ = _listen_offen(ll)
    bruecke = client.app
    bereiche = anordnung([False] * 3)
    try:
        # Passt ausser der Reihe (3 vor 1): completed = 1, die alte Rechnung saehe Aufgabe 2.
        r = client.post(f"/lernen/api/sitzungen/{SID}/passt", json={"nummer": 3, "vorbewertung_id": _vid(3)})
        assert r.status_code == 200 and r.json()["weiter_zu"] == 1
        assert bruecke._sitzungen[SID].nummer == 1
        vorher = len(penecho.aufrufe)
        r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ablauf"] == "liste" and body["aktuell"] == 1 and body["nummer"] == 1
        assert [a["status"] for a in body["aufgaben"]] == ["offen", "offen", "bestaetigt"]
        neu = penecho.aufrufe[vorher:]
        assert "aufgabe_setzen" not in [a[0] for a in neu]
        assert ("bereich_zeigen", "pe-1", bereiche[0]) in neu
        assert all(r is not None for _, r in penecho.rechtecke)  # nie das alte Raster
        assert bruecke._sitzungen[SID].nummer == 1
    finally:
        bruecke.beenden()


def test_neustart_listensitzung_bekommt_keine_alte_nummer() -> None:
    ll = ListenLern(2)
    ll.eintraege[1]["status"] = "bestaetigt"  # ausser der Reihe: completed = 1, aktuell = 1
    client, _, penecho = bauen(ll.lern)
    try:
        assert _stand(client)["aktuell"] == 1
        sitzung = client.app._sitzungen[SID]
        assert sitzung.liste is True and sitzung.nummer is None
        r = client.post(f"/lernen/api/sitzungen/{SID}/weiter")
        assert r.status_code == 200 and r.json()["nummer"] == 1
        assert sitzung.nummer == 1 and "aufgabe_setzen" not in penecho.namen()
    finally:
        client.app.beenden()


def test_abgelehnte_alte_antwort_setzt_keine_alte_nummer() -> None:
    ll = ListenLern(2)
    ll.eintraege[1]["status"] = "bestaetigt"
    client, _, _ = _listen_offen(ll)
    try:
        sitzung = client.app._sitzungen[SID]
        assert sitzung.nummer == 1
        # Alter Body mit revision: Learning lehnt ohne attempt_id ab, `_stand_neu` antwortet 409.
        r = client.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"value": 1}})
        assert r.status_code == 409 and "turn" in r.json()
        assert sitzung.nummer == 1
    finally:
        client.app.beenden()


def test_zeichnung_im_listenkurs_abgelehnt() -> None:
    ll = ListenLern(2, typ="penecho_canvas")
    client, penecho, _ = _listen_offen(ll)
    try:
        r = client.post(f"/lernen/api/sitzungen/{SID}/zeichnung", json={"revision": 1})
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "nicht_im_listenmodus" and body["meldung"] == NICHT_IM_LISTENMODUS
        assert body["stand"]["ablauf"] == "liste"
        assert "schnappschuss" not in penecho.namen()
        assert "learning_canvas_open" not in ll.lern.tools()
    finally:
        client.app.beenden()


def test_kursende_bericht_eines_listenkurses_mit_rechtecken(tmp_path) -> None:
    datei = tmp_path / "kursende.json"
    datei.write_text(json.dumps({f"{ACTOR}|{KURS}": SID}), encoding="utf-8")
    ll = ListenLern(2)
    for e in ll.eintraege:
        e["status"] = "bestaetigt"
    ll.lern.antworten["learning_course_overview"] = _ueberblick(None)
    client, _, penecho = bauen(ll.lern, kursende=datei)
    try:
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
        assert r.status_code == 200 and r.json()["abgeschlossen"] is True
        bereiche = anordnung([False] * 2)
        assert penecho.bericht_rechtecke == [{1: bereiche[0], 2: bereiche[1]}]
    finally:
        client.app.beenden()

# ---- Teil A2, Task 6: Zusammenfassung als PDF und Textfassung -----------------------------------

def _zf_offen(ll: ListenLern, penecho: FakePenEcho | None = None, **kw: Any):
    client, penecho, _ = _listen_offen(ll, penecho, **kw)
    return client, penecho


def test_zusammenfassung_route() -> None:
    ll = ListenLern(2)
    client, penecho = _zf_offen(ll)
    try:
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
        assert re.fullmatch(r'attachment; filename="zusammenfassung-testkurs-\d{4}-\d{2}-\d{2}\.pdf"',
                            r.headers["content-disposition"])
        # Je Aufgabe ein Bild mit eigenem Rechteck; die ganze Fläche wird nicht aufgenommen.
        rechtecke = [rt for _, rt in penecho.aufnahme_rechtecke]
        assert rechtecke == anordnung([False] * 2)
    finally:
        client.app.beenden()


def test_zusammenfassung_nur_fuer_listenkurse_und_bekannte_sitzungen() -> None:
    lern = FakeLern({"learning_course_overview": _ueberblick(turn()), "learning_task_next": {"session": turn()}})
    client, _, _ = bauen(lern)
    try:
        assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 400
        assert client.get("/lernen/api/sitzungen/nicht-uuid/zusammenfassung.pdf").status_code == 404
    finally:
        client.app.beenden()


def test_zusammenfassung_ohne_flaeche_ist_trotzdem_ein_pdf() -> None:
    ll = ListenLern(2)
    client, penecho = _zf_offen(ll)
    try:
        penecho.fehler = PenEchoNichtErreichbar("weg")
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
    finally:
        client.app.beenden()


def test_zusammenfassung_zeitbudget_laesst_restliche_bilder_weg() -> None:
    ll = ListenLern(3)
    client, penecho = _zf_offen(ll)
    try:
        zeiten = iter([0.0, 1.0, 31.0, 32.0, 33.0, 34.0])
        client.app._uhr = lambda: next(zeiten)
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        # Aufgabe 1 noch im Budget; vor Aufgabe 2 ist es verbraucht, danach keine Aufnahme mehr.
        assert [n for n, _ in penecho.aufnahme_rechtecke] == [1]
    finally:
        client.app.beenden()


def test_zusammenfassung_zeitlimit_je_aufnahme_ist_der_rest_des_budgets() -> None:
    ll = ListenLern(2)
    client, penecho = _zf_offen(ll)
    try:
        zeiten = iter([0.0, 1.0, 12.0, 31.0, 40.0])
        client.app._uhr = lambda: next(zeiten)
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert penecho.aufnahme_timeouts == [29.0, 18.0]
    finally:
        client.app.beenden()


def test_zusammenfassung_nimmt_nur_die_aufgabenbereiche_auf() -> None:
    # Live 09.11.: das Bild der ganzen Flaeche kam bei 16 Aufgaben mit 74 x 888 Pixeln, unlesbar. Es entfaellt;
    # jede Aufgabe hat ihr eigenes Bild.
    ll = ListenLern(2)
    client, penecho = _zf_offen(ll)
    try:
        bereiche = anordnung([False] * 2)
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert [rt for _, rt in penecho.aufnahme_rechtecke] == bereiche
    finally:
        client.app.beenden()


def test_zusammenfassung_werkzeugfehler_einer_aufgabe_stoppt_nicht() -> None:
    ll = ListenLern(3)
    client, penecho = _zf_offen(ll)
    try:
        bereiche = anordnung([False] * 3)
        penecho.aufnahme_fehler = lambda rt: PenEchoNichtErreichbar("werkzeug") if rt == bereiche[0] else None
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert len(penecho.aufnahme_rechtecke) == 3
    finally:
        client.app.beenden()


@pytest.mark.parametrize(("fehler", "aufnahmen"), [
    # Bis zum Canvas je Aufgabe kamen CANVAS_NOT_VISIBLE und CANVAS_BUSY als Werkzeugfehler: nur dieses Bild fehlt.
    (PenEchoNichtSichtbar("CANVAS_NOT_VISIBLE"), 3),
    (PenEchoBeschaeftigt("CANVAS_BUSY"), 3),
    # ACTIVE_CANVAS_REQUIRED war schon vorher eine eigene Klasse und beendet die Aufnahmen wie bisher.
    (PenEchoNichtSichtbar("ACTIVE_CANVAS_REQUIRED"), 1),
])
def test_zusammenfassung_alte_sitzung_belegt_oder_unsichtbar_wie_werkzeugfehler(
    fehler: Exception, aufnahmen: int,
) -> None:
    ll = ListenLern(3)
    client, penecho = _zf_offen(ll)
    try:
        assert client.app._sitzungen[SID].je_aufgabe is False
        bereiche = anordnung([False] * 3)
        penecho.aufnahme_fehler = lambda rt: fehler if rt == bereiche[0] else None
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert len(penecho.aufnahme_rechtecke) == aufnahmen
    finally:
        client.app.beenden()


def test_zusammenfassung_verbindungsfehler_stoppt_weitere_aufnahmen() -> None:
    ll = ListenLern(3)
    client, penecho = _zf_offen(ll)
    try:
        penecho.aufnahme_fehler = lambda rt: PenEchoNichtErreichbar("verbindung")
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert len(penecho.aufnahme_rechtecke) == 1
    finally:
        client.app.beenden()


def test_zusammenfassung_ohne_gemerktes_board_oeffnet_und_legt_nichts_an() -> None:
    ll = ListenLern(2)
    client, penecho = _zf_offen(ll)
    try:
        client.app._sitzungen[SID].board = None
        vorher = len(penecho.aufrufe)
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        neu = penecho.aufrufe[vorher:]
        assert [a for a in neu if a[0] in ("sitzung", "vergessen", "schnappschuss")] == []
        assert client.app._sitzungen[SID].board is None
    finally:
        client.app.beenden()


def test_zusammenfassung_veraltete_sitzung_wird_nicht_neu_angelegt() -> None:
    ll = ListenLern(2)
    client, penecho = _zf_offen(ll)
    try:
        penecho.aufnahme_fehler = lambda rt: PenEchoSitzungUnbekannt("session_expired")
        vorher = len([a for a in penecho.aufrufe if a[0] == "sitzung"])
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert len([a for a in penecho.aufrufe if a[0] == "sitzung"]) == vorher
        assert len(penecho.aufnahme_rechtecke) == 1
    finally:
        client.app.beenden()


def _jpeg() -> bytes:
    from PIL import Image

    puffer = io.BytesIO()
    Image.new("RGB", (8, 6), (200, 30, 30)).save(puffer, format="JPEG")
    return puffer.getvalue()


def _zf_mitschneiden(monkeypatch) -> list:
    """Faengt die Zusammenfassung ab, die an `als_pdf` geht (das echte PDF entsteht trotzdem)."""
    import spaces.learning.huelle.bruecke as bruecke_modul

    gesehen: list = []
    echt = bruecke_modul.als_pdf
    monkeypatch.setattr(bruecke_modul, "als_pdf", lambda z: (gesehen.append(z), echt(z))[1])
    return gesehen


def test_zusammenfassung_jpeg_aufnahmen_kommen_als_png_ins_pdf(monkeypatch) -> None:
    # Abnahme A2: PenEcho liefert grosse Ausschnitte als JPEG/WebP, das PDF liest nur PNG -> 17x "Kein Bild".
    gesehen = _zf_mitschneiden(monkeypatch)
    ll = ListenLern(2)
    client, penecho = _zf_offen(ll)
    try:
        penecho.bild = _jpeg()
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        (z,) = gesehen
        bilder = [a.bild for a in z.aufgaben]
        assert len(bilder) == 2
        assert all(b is not None and b.startswith(b"\x89PNG\r\n\x1a\n") for b in bilder)
    finally:
        client.app.beenden()


def test_zusammenfassung_unlesbares_bild_faellt_einzeln_weg_mit_warnung(monkeypatch, caplog) -> None:
    gesehen = _zf_mitschneiden(monkeypatch)
    ll = ListenLern(2)
    client, penecho = _zf_offen(ll)
    try:
        penecho.bild = b"kein bild"
        with caplog.at_level("WARNING"):
            assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert len(penecho.aufnahme_rechtecke) == 2  # jede Aufnahme versucht, keine bricht die Reihe ab
        (z,) = gesehen
        assert all(a.bild is None for a in z.aufgaben)
        assert "Bild unlesbar" in caplog.text
    finally:
        client.app.beenden()


def test_zusammenfassung_zeigt_getippte_antworten(monkeypatch) -> None:
    # Live 09.11.: getippte Antworten standen als „eingetippt“ im PDF. Learning liefert sie jetzt mit:
    # in der Vorbewertung (offen) oder am Eintrag (gebucht ohne Vorbewertung).
    gesehen = _zf_mitschneiden(monkeypatch)
    ll = ListenLern(3)
    ll.eintraege[0]["vorbewertung"] = {**_vor(vid=_vid(1), gelesen=None, quelle="getippt"), "antwort": {"text": "x = 2"}}
    ll.eintraege[1]["status"] = "bestaetigt"
    ll.eintraege[1]["antwort"] = {"value": 0.25}
    client, _ = _zf_offen(ll)
    try:
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        (z,) = gesehen
        assert [a.ergebnis for a in z.aufgaben] == ["x = 2", "0,25", "offen"]
    finally:
        client.app.beenden()


def test_zusammenfassung_nennt_nicht_bearbeitete(monkeypatch) -> None:
    # Abnahme 13.11. (F6): leer gebuchte Aufgaben hiessen „eingegeben“, die KI-Kursrueckmeldung lobte sie als Staerke.
    from spaces.learning.huelle.kursrueckmeldung import rueckmeldungsdaten

    gesehen = _zf_mitschneiden(monkeypatch)
    ll = ListenLern(3)
    ll.eintraege[0]["status"] = "bestaetigt"
    ll.eintraege[0]["antwort"] = {}  # von „Kurs abgeben“ leer gebucht, nie bearbeitet
    ll.eintraege[1]["status"] = "bestaetigt"
    ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(2), lesbar=False, gelesen=None)  # unlesbar erneut abgegeben
    client, _ = _zf_offen(ll)
    try:
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        (z,) = gesehen
        assert [a.ergebnis for a in z.aufgaben] == ["nicht bearbeitet", "nicht bearbeitet", "offen"]
        assert [a.kernaussage for a in z.aufgaben] == ["Nicht bearbeitet – 0 Punkte."] * 2 + ["–"]
        daten = rueckmeldungsdaten(z.kurs, z.aufgaben, [])
        assert [(a["ergebnis"], a["rueckmeldung"]) for a in daten["aufgaben"][:2]] == [
            ("nicht bearbeitet", "Nicht bearbeitet – 0 Punkte.")] * 2
    finally:
        client.app.beenden()


def test_zusammenfassung_begriffe_ohne_slug_dubletten(monkeypatch) -> None:
    # Abnahme A2 (Befund 6): `ablauf-x` und `ablauf_x` standen doppelt im PDF; die Seite fasst sie schon zusammen.
    gesehen = _zf_mitschneiden(monkeypatch)
    ll = ListenLern(2)
    begriffe = [{"concept_key": "k1", "titel": "ablauf-kurvendiskussion", "beherrschung": 0.2},
                {"concept_key": "k2", "titel": "ablauf_kurvendiskussion", "beherrschung": 0.6},
                {"concept_key": "k3", "titel": "Ableitung", "beherrschung": None}]
    ll.lern.antworten["learning_course_overview"] = lambda kw: {**_ueberblick(ll.turn()), "begriffe": begriffe}
    client, _ = _zf_offen(ll)
    try:
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        (z,) = gesehen
        assert z.begriffe == (("Ablauf Kurvendiskussion", "60 %"), ("Ableitung", "noch offen"))
    finally:
        client.app.beenden()


def test_zusammenfassung_ohne_gemerktes_board_warnt(caplog) -> None:
    ll = ListenLern(2)
    client, _ = _zf_offen(ll)
    try:
        client.app._sitzungen[SID].board = None
        with caplog.at_level("WARNING"):
            assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert "kein gemerktes Board" in caplog.text
    finally:
        client.app.beenden()


def _md_dateien(ordner) -> list:
    return sorted(ordner.glob("*.md"))


def test_markdown_nur_mit_ordner(tmp_path, monkeypatch) -> None:
    import dataclasses

    from spaces.learning.huelle import bruecke as modul

    aufrufe: list[object] = []
    echt = modul.als_markdown
    monkeypatch.setattr(modul, "als_markdown", lambda z: (aufrufe.append(z), echt(z))[1])
    ll = ListenLern(2)
    client, _ = _zf_offen(ll)
    try:
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert aufrufe == []  # ohne Einstellung wird die Textfassung nicht einmal gebaut
    finally:
        client.app.beenden()
    ll = ListenLern(2)
    ordner = tmp_path / "wissen"
    client, _ = _zf_offen(ll, einstellung=dataclasses.replace(E, zusammenfassung_ordner=str(ordner)))
    try:
        for _ in range(2):  # gleicher Tag überschreibt
            assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        (datei,) = _md_dateien(ordner)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-testkurs\.md", datei.name)
        text = datei.read_text(encoding="utf-8")
        assert ACTOR not in text and SID not in text and "## Aufgabe 1" in text and "## Begriffe" in text
        assert not list(ordner.glob("*.tmp"))
    finally:
        client.app.beenden()


def test_markdown_schreibfehler_bricht_das_pdf_nicht(tmp_path) -> None:
    import dataclasses

    blockiert = tmp_path / "datei"
    blockiert.write_text("x", encoding="utf-8")  # ein Ordner, der keiner sein kann
    ll = ListenLern(2)
    client, _ = _zf_offen(ll, einstellung=dataclasses.replace(E, zusammenfassung_ordner=str(blockiert / "unten")))
    try:
        r = client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
    finally:
        client.app.beenden()


def test_markdown_beim_kursende(tmp_path) -> None:
    import dataclasses

    datei = tmp_path / "kursende.json"
    datei.write_text(json.dumps({f"{ACTOR}|{KURS}": SID}), encoding="utf-8")
    ordner = tmp_path / "wissen"
    ll = ListenLern(2)
    for e in ll.eintraege:
        e["status"] = "bestaetigt"
    ll.lern.antworten["learning_course_overview"] = _ueberblick(None)
    client, _, _ = bauen(ll.lern, kursende=datei, einstellung=dataclasses.replace(E, zusammenfassung_ordner=str(ordner)))
    try:
        assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()["abgeschlossen"] is True
        (md,) = _md_dateien(ordner)
        text = md.read_text(encoding="utf-8")
        assert "Ergebnis: eingegeben" in text and SID not in text
    finally:
        client.app.beenden()


# ---- Schlussreview I1: eine Karte, die nicht auf die Flaeche kommt, zeigt die Leiste als Text -------------

def _karte_verweigern(penecho: FakePenEcho) -> list[bool]:
    schalter = [True]  # True = PenEcho lehnt die Karte ab
    echt = penecho.rueckmeldung_zeigen

    def zeigen(*a: Any, **k: Any) -> None:
        if schalter[0]:
            raise PenEchoNichtErreichbar("Platz belegt")
        echt(*a, **k)

    penecho.rueckmeldung_zeigen = zeigen  # type: ignore[method-assign]
    return schalter


def test_liste_hintergrundkarte_abgelehnt_stand_traegt_den_kartentext() -> None:
    ll = ListenLern(2)
    penecho = FakePenEcho()
    schalter = _karte_verweigern(penecho)
    client, penecho, _ = _listen_offen(ll, penecho)
    bruecke = client.app
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2}).status_code == 202
        _bis(lambda: _ruhig(bruecke), 2)
        eintrag = _stand(client)["aufgaben"][1]
        assert eintrag["status"] == "rueckmeldung"
        assert eintrag["karte"].startswith("Gelesen: 2") and "Richtig." in eintrag["karte"]
        assert "karte" not in _stand(client)["aufgaben"][0]
        # Neue Abgabe: der alte Text gilt nicht mehr; landet die neue Karte, bleibt er weg.
        schalter[0] = False
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2}).status_code == 202
        _bis(lambda: _ruhig(bruecke), 2)
        assert "karte" not in _stand(client)["aufgaben"][1]
    finally:
        bruecke.beenden()


def test_liste_oeffnen_karte_abgelehnt_stand_traegt_den_kartentext() -> None:
    ll = ListenLern(2)
    ll.eintraege[0]["vorbewertung"] = _vor(vid=_vid(1), gelesen="7")
    penecho = FakePenEcho()
    _karte_verweigern(penecho)
    client, _, offen = _listen_offen(ll, penecho)
    try:
        eintrag = offen["stand"]["aufgaben"][0]
        assert eintrag["status"] == "rueckmeldung" and eintrag["karte"].startswith("Gelesen: 7")
        assert _stand(client)["aufgaben"][0]["karte"] == eintrag["karte"]
    finally:
        client.app.beenden()


# ---- Schlussreview I2: der beendete Listenkurs meldet sein Kennzeichen beim erneuten Oeffnen -----------------

def test_beendeter_listenkurs_oeffnen_meldet_ablauf_liste(tmp_path) -> None:
    ll = ListenLern(1)
    ll.eintraege[0]["vorbewertung"] = _vor(vid=_vid(1))
    client, _, _ = _listen_offen(ll, kursende=tmp_path / "kursende.json")
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/passt",
                           json={"nummer": 1, "vorbewertung_id": _vid(1)}).status_code == 200
        ll.lern.antworten["learning_course_overview"] = _ueberblick(None)
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["abgeschlossen"] is True and body["turn"]["state"] == "completed"
        assert body["stand"]["ablauf"] == "liste"
    finally:
        client.app.beenden()


def test_beendeter_anpassender_kurs_oeffnen_ohne_stand() -> None:
    _, client, _ = _bis_zum_ende()
    body = client.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()
    assert body["abgeschlossen"] is True and "stand" not in body


# ---- Schlussreview Runde 2: die Seite fragt weiter, solange die Karte noch unterwegs ist ------------------

def _karte_haelt_und_scheitert(penecho: FakePenEcho) -> threading.Event:
    """Die Flaechenspur haengt in `rueckmeldung_zeigen`, bis das Ereignis gesetzt ist; dann lehnt PenEcho ab."""
    frei = threading.Event()

    def zeigen(*_a: Any, **_k: Any) -> None:
        assert frei.wait(5), "Karte nie freigegeben"
        raise PenEchoNichtErreichbar("Platz belegt")

    penecho.rueckmeldung_zeigen = zeigen  # type: ignore[method-assign]
    return frei


def test_liste_karte_unterwegs_stand_meldet_karte_ausstehend() -> None:
    ll = ListenLern(2)
    penecho = FakePenEcho()
    client, penecho, _ = _listen_offen(ll, penecho)
    bruecke = client.app
    frei = _karte_haelt_und_scheitert(penecho)
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2}).status_code == 202
        _bis(lambda: bruecke._modellspur.wartend(f"{SID}:abgabe:") == 0
             and bruecke._flaechenspur.zustand(f"{SID}:rueckmeldung:2") == "laeuft", 2)
        eintrag = _stand(client)["aufgaben"][1]
        assert eintrag["status"] == "rueckmeldung" and eintrag["karte_ausstehend"] is True
        assert "karte" not in eintrag
        frei.set()
        _bis(lambda: _ruhig(bruecke), 2)
        eintrag = _stand(client)["aufgaben"][1]
        assert eintrag["karte"].startswith("Gelesen: 2") and "karte_ausstehend" not in eintrag
    finally:
        frei.set()
        bruecke.beenden()


def test_liste_karte_endet_zwischen_den_lesungen_text_kommt_trotzdem() -> None:
    """Die Spur legt den Text ab und raeumt ihre Arbeit weg, nachdem `_stand_aus` die Texte gelesen hat:
    weder `karte` noch `karte_ausstehend` waere sonst gesetzt, und die Seite hoerte auf zu fragen."""
    ll = ListenLern(2)
    penecho = FakePenEcho()
    _karte_verweigern(penecho)
    client, penecho, _ = _listen_offen(ll, penecho)
    bruecke = client.app
    try:
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"nummer": 2}).status_code == 202
        _bis(lambda: _ruhig(bruecke), 2)
        echt = bruecke._kartentexte_von
        aufrufe = [0]

        def erst_leer(sid: str) -> dict[int, str]:
            aufrufe[0] += 1
            return {} if aufrufe[0] == 1 else echt(sid)  # erste Lesung vor dem Ablegen des Textes

        bruecke._kartentexte_von = erst_leer  # type: ignore[method-assign]
        eintrag = _stand(client)["aufgaben"][1]
        assert eintrag["status"] == "rueckmeldung"
        assert eintrag.get("karte", "").startswith("Gelesen: 2") or eintrag.get("karte_ausstehend") is True
    finally:
        bruecke.beenden()


def test_liste_oeffnen_karte_der_zweiten_aufgabe_unterwegs_karte_ausstehend() -> None:
    ll = ListenLern(2)
    ll.eintraege[1]["vorbewertung"] = _vor(vid=_vid(2), gelesen="7")
    penecho = FakePenEcho()
    frei = _karte_haelt_und_scheitert(penecho)
    client, _, _ = bauen(ll.lern, penecho)
    bruecke = client.app
    try:
        r = client.post(f"/lernen/api/kurse/{KURS}/oeffnen")
        assert r.status_code == 200, r.text
        eintrag = r.json()["stand"]["aufgaben"][1]
        assert eintrag["status"] == "rueckmeldung" and eintrag["karte_ausstehend"] is True
        frei.set()
        _bis(lambda: bruecke._flaechenspur.wartend(f"{SID}:setzen:") == 0, 2)
        eintrag = _stand(client)["aufgaben"][1]
        assert eintrag["karte"].startswith("Gelesen: 7") and "karte_ausstehend" not in eintrag
    finally:
        frei.set()
        bruecke.beenden()


# ---- Teil B: Person je Anfrage, Kurse je Person -------------------------------------------------------

LH = "http://learnhouse.test"
KURS_P = "55555555-5555-4555-8555-555555555555"
SID_PA = "66666666-6666-4666-8666-666666666661"
SID_PB = "66666666-6666-4666-8666-666666666662"
E_LH = dataclasses.replace(E, learnhouse_url=LH)


class LearnHouse:
    """Dienstroute `GET /api/v1/learning/personen/{actor}/kurse` (Task 2 im Fork)."""

    def __init__(self) -> None:
        self.kurse: dict[str, list[dict[str, str]]] = {
            "lh-1-person-a": [{"course_id": KURS_P, "titel": "Kurs B"}],
            "lh-1-person-b": [{"course_id": KURS_P, "titel": "Kurs B"}],
        }
        self.weg = False
        self.anfragen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-LearnHouse-Learning-Service-Key"] == SCHLUESSEL
        actor = request.url.path.split("/")[-2]
        self.anfragen.append(actor)
        if self.weg:
            raise httpx.ConnectError("weg")
        if actor not in self.kurse:
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json={"kurse": self.kurse[actor]})


class Uhr:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _katalog(lh: LearnHouse, uhr: Any = None) -> Katalog:
    return Katalog(E_LH, httpx.Client(transport=httpx.MockTransport(lh)), uhr=uhr or time.monotonic)


def _person_lern() -> FakeLern:
    return FakeLern({"learning_course_overview": _ueberblick(turn(course_id=KURS_P)),
                     "learning_task_next": {"session": turn(course_id=KURS_P)}})


def _envelope_mcp(gesehen: list[dict[str, Any]]):
    """learning-mcp: jede Person hat ihre eigene Sitzung im Kurs B."""
    def mcp(request: httpx.Request) -> httpx.Response:
        rpc = json.loads(request.content)
        event = rpc["params"]["arguments"]
        gesehen.append(event)
        sid = {"lh-1-person-a": SID_PA, "lh-1-person-b": SID_PB}.get(event["actor"]["actor_id"], SID)
        kurs = event.get("course_id", KURS_P)
        sitzung = {**turn(course_id=kurs), "session_id": sid}
        result = (_ueberblick(sitzung) if rpc["params"]["name"] == "learning_course_overview"
                  else {"session": sitzung})
        text = json.dumps({"state": "completed", "result": result})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": text}], "isError": False}})
    return mcp


def _ganz(einstellung: Any, lh: LearnHouse | None, umschlaege: list[dict[str, Any]],
          api_koepfe: list[httpx.Headers], penecho: Any = None) -> Weiche:
    """Weiche + Bruecke wie im Server, mit Fakes fuer LearnHouse, learning-mcp, learning-api und PenEcho."""
    def api(request: httpx.Request) -> httpx.Response:
        api_koepfe.append(request.headers)
        return httpx.Response(200, json={"notes": []})

    async def seite(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    lern = LernMcp("http://learning-mcp.test/mcp", transport=httpx.MockTransport(_envelope_mcp(umschlaege)))
    katalog = Katalog(einstellung, httpx.Client(transport=httpx.MockTransport(lh or LearnHouse())))
    bruecke = Bruecke(einstellung, lern, penecho or FakePenEcho(), Sperre(),
                      httpx.Client(transport=httpx.MockTransport(api)), katalog=katalog)  # type: ignore[arg-type]
    return Weiche(einstellung, seite, bruecke)


def _browser(weiche: Weiche, cookie: str) -> TestClient:
    return TestClient(weiche, base_url="https://lernen.example.test",
                      headers={"cookie": cookie, "origin": "https://lernen.example.test"})


def test_actor_kommt_aus_dem_cookie() -> None:
    umschlaege: list[dict[str, Any]] = []
    koepfe: list[httpx.Headers] = []
    lh = LearnHouse()
    weiche = _ganz(E_LH, lh, umschlaege, koepfe)
    a = _browser(weiche, f"{PERSON_COOKIE}={person_cookie(SCHLUESSEL, 'lh-1-person-a')}")
    b = _browser(weiche, f"{PERSON_COOKIE}={person_cookie(SCHLUESSEL, 'lh-1-person-b')}")
    for browser, actor, sid in ((a, "lh-1-person-a", SID_PA), (b, "lh-1-person-b", SID_PB)):
        vorher = len(umschlaege)
        assert browser.get("/lernen/api/kurse").json() == [{"course_id": KURS_P, "titel": "Kurs B"}]
        r = browser.post(f"/lernen/api/kurse/{KURS_P}/oeffnen")
        assert r.status_code == 200, r.text
        assert r.json()["turn"]["session_id"] == sid
        assert browser.get(f"/lernen/api/notizen?course_id={KURS_P}").status_code == 200
        assert {u["actor"]["actor_id"] for u in umschlaege[vorher:]} == {actor}
        assert koepfe[-1]["X-Learning-Actor"] == actor
    assert lh.anfragen and set(lh.anfragen) == {"lh-1-person-a", "lh-1-person-b"}
    # Beide Sitzungen bleiben je Person erhalten: A arbeitet nach dem Oeffnen von B in ihrer eigenen weiter.
    vorher = len(umschlaege)
    assert a.post(f"/lernen/api/sitzungen/{SID_PA}/weiter").status_code == 200
    assert {u["actor"]["actor_id"] for u in umschlaege[vorher:]} == {"lh-1-person-a"}


def test_notweg_nutzt_inhaber_kennung() -> None:
    for einstellung in (E, E_LH):  # ohne und mit LearnHouse: der Telegram-Zugang bleibt der Inhaber
        umschlaege: list[dict[str, Any]] = []
        koepfe: list[httpx.Headers] = []
        lh = LearnHouse()
        weiche = _ganz(einstellung, lh, umschlaege, koepfe)
        inhaber = _browser(weiche, f"{COOKIE}={token_erzeugen(SCHLUESSEL)}")
        assert inhaber.get("/lernen/api/kurse").json() == [{"course_id": KURS, "titel": "Testkurs"}]
        assert inhaber.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 200
        assert inhaber.get(f"/lernen/api/notizen?course_id={KURS}").status_code == 200
        assert umschlaege and {u["actor"]["actor_id"] for u in umschlaege} == {ACTOR}
        assert koepfe[-1]["X-Learning-Actor"] == ACTOR
        # Ohne LearnHouse keine Anfrage; mit LearnHouse hoechstens eine (Fehlschlag 60 s gepuffert),
        # und die Kurse des Notwegs bleiben, auch wenn LearnHouse die Kennung nicht kennt.
        assert len(lh.anfragen) == (0 if einstellung is E else 1)


def test_bruecke_ohne_kennung_der_weiche_403() -> None:
    lern = FakeLern({})
    bruecke = Bruecke(E, lern, FakePenEcho(), Sperre(), httpx.Client(transport=httpx.MockTransport(_api_leer)))  # type: ignore[arg-type]
    client = TestClient(bruecke)
    assert client.get("/lernen/api/kurse").status_code == 403
    assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").status_code == 403
    for falsch in ("", "a b", "x" * 200, 7):
        client.app_state["actor_id"] = falsch
        assert client.get("/lernen/api/kurse").status_code == 403, falsch
    assert lern.aufrufe == []


def test_entfernte_person_kommt_nicht_mehr_rein() -> None:
    lh, uhr = LearnHouse(), Uhr()
    lern = _person_lern()
    client, _, _ = bauen(lern, einstellung=E_LH, katalog=_katalog(lh, uhr), actor="lh-1-person-a")
    assert client.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    assert set(lern.akteure) == {"lh-1-person-a"}
    del lh.kurse["lh-1-person-a"]  # im Fork aus dem Anbieter entfernt (Dienstroute jetzt 404)
    uhr.t += 30
    assert client.get("/lernen/api/kurse").status_code == 200  # Puffer, hoechstens 60 s
    uhr.t += 30
    kein_zugang = {"fehler": "kein_zugang", "text": KEIN_ZUGANG_MEHR, "meldung": KEIN_ZUGANG_MEHR}
    vorher = len(lern.aufrufe)
    for r in (client.get("/lernen/api/kurse"), client.post(f"/lernen/api/kurse/{KURS_P}/oeffnen"),
              client.post(f"/lernen/api/kurse/{KURS_P}/neu"), client.post(f"/lernen/api/sitzungen/{SID}/weiter"),
              client.post(f"/lernen/api/sitzungen/{SID}/hinweis")):
        assert r.status_code == 403 and r.json() == kein_zugang
    assert len(lern.aufrufe) == vorher


def test_fremde_sitzung_nicht_sichtbar() -> None:
    lh = LearnHouse()
    lern = _person_lern()
    client_a, _, _ = bauen(lern, einstellung=E_LH, katalog=_katalog(lh), actor="lh-1-person-a")
    assert client_a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    client_b = als(TestClient(client_a.app), "lh-1-person-b")
    vorher = len(lern.aufrufe)
    for r in (client_b.post(f"/lernen/api/sitzungen/{SID}/weiter"), client_b.get(f"/lernen/api/sitzungen/{SID}/stand"),
              client_b.post(f"/lernen/api/sitzungen/{SID}/hinweis"),
              client_b.post(f"/lernen/api/sitzungen/{SID}/antwort", json={"revision": 1, "answer": {"choice_id": "a"}})):
        assert r.status_code == 404, r.text
    assert len(lern.aufrufe) == vorher  # die gemerkte Sitzung von A geht nie unter B an Learning
    assert client_a.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 200
    # Nicht im Speicher (etwa nach einem Neustart): Learning wird als B gefragt und lehnt ab -> 404.
    lern2 = FakeLern({"learning_task_next": LearningAbgelehnt("session belongs to another learner",
                                                               "session_access_denied")})
    client_b2, _, _ = bauen(lern2, einstellung=E_LH, katalog=_katalog(lh), actor="lh-1-person-b")
    assert client_b2.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 404
    assert lern2.akteure == ["lh-1-person-b"]


def test_uebersicht_weg_503() -> None:
    lh = LearnHouse()
    lh.weg = True
    lern = _person_lern()
    client, _, _ = bauen(lern, einstellung=E_LH, katalog=_katalog(lh), actor="lh-1-person-a")
    weg = {"fehler": "uebersicht_weg", "text": UEBERSICHT_WEG, "meldung": UEBERSICHT_WEG}
    for r in (client.get("/lernen/api/kurse"), client.post(f"/lernen/api/kurse/{KURS_P}/oeffnen"),
              client.post(f"/lernen/api/kurse/{KURS_P}/neu")):
        assert r.status_code == 503 and r.json() == weg
    assert lern.aufrufe == []


def test_fremder_kurs_403() -> None:
    lh = LearnHouse()
    lern = _person_lern()
    client, _, _ = bauen(lern, einstellung=E_LH, katalog=_katalog(lh), actor="lh-1-person-a")
    kein_zugang = {"fehler": "kein_zugang", "text": KEIN_ZUGANG_MEHR, "meldung": KEIN_ZUGANG_MEHR}
    # KURS ist der Kurs des Notwegs, aber nicht in der Liste von person-a.
    for r in (client.post(f"/lernen/api/kurse/{KURS}/oeffnen"), client.post(f"/lernen/api/kurse/{KURS}/neu"),
              client.get(f"/lernen/api/kurse/{KURS}/bewertung")):
        assert r.status_code == 403 and r.json() == kein_zugang
    assert lern.aufrufe == []
    # Eine Sitzung, die Learning person-a in einem fremden Kurs nennt, bleibt unsichtbar.
    lern2 = FakeLern({"learning_task_next": {"session": turn(course_id=KURS)}})
    client2, _, _ = bauen(lern2, einstellung=E_LH, katalog=_katalog(lh), actor="lh-1-person-a")
    assert client2.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 404


def test_kursende_je_person(tmp_path) -> None:
    lh = LearnHouse()
    datei = tmp_path / "kursende.json"
    ende = turn(None, state="completed", course_id=KURS_P)
    lern = FakeLern({"learning_course_overview": _ueberblick(turn(course_id=KURS_P)),
                     "learning_task_next": [{"session": turn(course_id=KURS_P)}, {"session": ende}]})
    client, _, _ = bauen(lern, einstellung=E_LH, katalog=_katalog(lh), actor="lh-1-person-a", kursende=datei)
    assert client.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    assert client.post(f"/lernen/api/sitzungen/{SID}/weiter").status_code == 200
    assert json.loads(datei.read_text(encoding="utf-8")) == {f"lh-1-person-a|{KURS_P}": SID}


def test_lern_mcp_kennung_je_aufruf() -> None:
    gesehen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(json.loads(request.content)["params"]["arguments"]["actor"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": json.dumps({"result": {}})}], "isError": False}})

    ohne = LernMcp("http://m.test/mcp", transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        ohne.rufe("learning_task_next", "x")  # ohne Kennung kein Aufruf, nie stillschweigend der Inhaber
    ohne.rufe("learning_task_next", "x", actor_id="lh-1-person-a")
    LernMcp("http://m.test/mcp", ACTOR, transport=httpx.MockTransport(handler)).rufe(
        "learning_task_next", "x", actor_id="lh-1-person-b")
    assert gesehen == [{"actor_id": "lh-1-person-a", "actor_type": "local_user"},
                       {"actor_id": "lh-1-person-b", "actor_type": "local_user"}]


# ---- Teil B, Fix-Runde 1 -------------------------------------------------------------------------

PA, PB = "lh-1-person-a", "lh-1-person-b"
SID_X = "66666666-6666-4666-8666-666666666669"


def _zwei_personen(mode: str = "training") -> tuple[FakeLern, TestClient, TestClient, LearnHouse, Uhr, Sperre]:
    """A und B im selben Kurs KURS_P, jede mit eigener Sitzung (Learning antwortet je Kennung)."""
    lern: FakeLern

    def sitzung_von(actor: str) -> dict[str, Any]:
        return {**turn(course_id=KURS_P, mode=mode), "session_id": {PA: SID_PA, PB: SID_PB}[actor]}

    def naechste(kw: dict[str, Any]) -> dict[str, Any]:
        actor = {SID_PA: PA, SID_PB: PB}.get(kw["session_id"])
        if actor != lern.akteure[-1]:
            raise LearningAbgelehnt("session belongs to another learner", "session_access_denied")
        return {"session": sitzung_von(actor)}

    lern = FakeLern({"learning_course_overview": lambda kw: _ueberblick(sitzung_von(lern.akteure[-1])),
                     "learning_task_next": naechste,
                     "learning_hint_request": {"hint": "Denken Sie an X."}})
    lh, uhr, sperre = LearnHouse(), Uhr(), Sperre()
    a, _, _ = bauen(lern, einstellung=E_LH, katalog=_katalog(lh, uhr), actor=PA, sperre=sperre)
    b = als(TestClient(a.app), PB)
    return lern, a, b, lh, uhr, sperre


def test_frei_entfernte_person_sperrt_die_ki_nicht_fuer_alle() -> None:
    lern, a, b, lh, uhr, sperre = _zwei_personen()
    assert a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    assert b.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    assert sperre.gesperrt() is False  # B uebt, A uebt: KI frei
    bruecke = a.app
    # A wird entfernt; eine Arbeit von A (z. B. Hintergrund nach Ablauf des Puffers) gibt danach frei.
    del lh.kurse[PA]
    uhr.t += 60
    sperre.offen("eben-gebucht")
    bruecke._frei(bruecke._sitzungen[SID_PA])
    assert sperre.gesperrt() is False  # B hat keine Pruefungsaufgabe offen: die KI bleibt fuer B frei
    # Faellt dagegen LearnHouse aus, bleibt es zu (unbekannte Kurse koennten eine Pruefungsaufgabe haben).
    lh.weg = True
    sperre.offen("eben-gebucht")
    bruecke._frei(bruecke._sitzungen[SID_PB])
    assert sperre.gesperrt() is True


def test_learnhouse_weg_gemerkte_sitzung_laeuft_ungemerkte_503() -> None:
    lern, a, _, lh, uhr, _ = _zwei_personen()
    assert a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    lh.weg = True
    uhr.t += 60
    r = a.post(f"/lernen/api/sitzungen/{SID_PA}/weiter")
    assert r.status_code == 200, r.text
    assert a.post(f"/lernen/api/sitzungen/{SID_PA}/hinweis").json() == {"hinweis": "Denken Sie an X."}
    # Nicht gemerkt (Huelle neu gestartet): Learning kennt As Sitzung, der Kurs ist aber nicht pruefbar -> 503.
    neu, _, _ = bauen(lern, einstellung=E_LH, katalog=_katalog(lh, uhr), actor=PA)
    r = neu.post(f"/lernen/api/sitzungen/{SID_PA}/weiter")
    assert r.status_code == 503
    assert r.json() == {"fehler": "uebersicht_weg", "text": UEBERSICHT_WEG, "meldung": UEBERSICHT_WEG}
    assert SID_PA not in neu.app._sitzungen
    # Eine fremde oder unbekannte Sitzung bleibt 404 (Learning lehnt sie fuer A ab).
    assert neu.post(f"/lernen/api/sitzungen/{SID_X}/weiter").status_code == 404


def test_gemerkte_sitzung_kurs_aus_der_liste_gefallen_403() -> None:
    lern, a, _, lh, uhr, _ = _zwei_personen()
    assert a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    lh.kurse[PA] = [{"course_id": KURS, "titel": "Anderer Kurs"}]  # noch Mitglied, Kurs P nicht mehr
    uhr.t += 60
    vorher = len(lern.aufrufe)
    kein_zugang = {"fehler": "kein_zugang", "text": KEIN_ZUGANG_MEHR, "meldung": KEIN_ZUGANG_MEHR}
    for r in (a.post(f"/lernen/api/sitzungen/{SID_PA}/weiter"), a.post(f"/lernen/api/sitzungen/{SID_PA}/hinweis"),
              a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen")):
        assert r.status_code == 403 and r.json() == kein_zugang
    assert len(lern.aufrufe) == vorher


def test_oeffnen_durch_b_laesst_a_sitzung_und_hintergrund() -> None:
    lern, a, b, _, _, _ = _zwei_personen()
    bruecke = a.app
    assert a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").json()["turn"]["session_id"] == SID_PA
    assert b.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").json()["turn"]["session_id"] == SID_PB
    # Bs Oeffnen raeumt nur Bs aeltere Sitzungen ab: As Sitzung bleibt gemerkt und gehoert A.
    assert bruecke._sitzungen[SID_PA].actor == PA and bruecke._sitzungen[SID_PB].actor == PB
    # Je Person die zuletzt geoeffnete Sitzung: As Hintergrundarbeiten laufen nach Bs Oeffnen weiter.
    assert bruecke._zuletzt == {PA: SID_PA, PB: SID_PB}
    assert bruecke._noch_offen(bruecke._sitzungen[SID_PA]) is True
    assert bruecke._noch_offen(bruecke._sitzungen[SID_PB]) is True
    # As naechster Schritt geht als A an Learning, mit As Sitzung (kein Neuaufbau ueber Learning als B).
    vorher = len(lern.aufrufe)
    assert a.post(f"/lernen/api/sitzungen/{SID_PA}/weiter").status_code == 200
    aufrufe = list(zip(lern.akteure[vorher:], (kw.get("session_id") for _, kw in lern.aufrufe[vorher:])))
    assert (PA, SID_PA) in aufrufe
    # Jede Sitzung geht nur unter ihrer eigenen Kennung an Learning (auch Bs, die `_frei` nachliest).
    assert all({SID_PA: PA, SID_PB: PB}[sid] == akt for akt, sid in aufrufe if sid is not None)


# ---- Teil B, Task 6: jede Person arbeitet auf der Flaeche ihres Tabs ---------------------------------------

KA = "lf-" + "a" * 32
KA2 = "lf-" + "c" * 32
KB = "lf-" + "b" * 32
KOPF = "X-Lernflaeche"


class TabMcp:
    """PenEchos MCP (direct HTTP) mit Tabs: start_session bindet an die canvasId, sonst an `ohne`.

    Je Werkzeugaufruf (name, arguments); so sieht der Test, an welche sessionId (= welchen Tab) jede
    Aenderung ging. Ein Board je Tab (`doc-<tab>`); mit documentId bleibt es dieses Board.
    """

    def __init__(self) -> None:
        self.aufrufe: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}},
                                  headers={"mcp-session-id": "m" * 64})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        name, args = body["params"]["name"], body["params"]["arguments"]
        self.aufrufe.append((name, args))
        if name == "penecho_start_session":
            tab = args.get("canvasId", "ohne")
            sc: dict[str, Any] = {"sessionId": "pe-" + tab, "documentId": args.get("documentId") or "doc-" + tab}
        elif name == "penecho_inspect_session":
            sc = {"sessionId": args["sessionId"], "documentId": "doc-" + args["sessionId"].removeprefix("pe-")}
        elif name == "penecho_read_file":
            sc = {"content": json.dumps({"revision": 1})}
        elif name == "penecho_list_files":
            sc = {"entries": []}
        else:
            sc = {"applied": True, "objectId": "text-box-1"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "structuredContent": sc, "content": [{"type": "text", "text": json.dumps(sc)}]}})

    def starts(self) -> list[dict[str, Any]]:
        return [a for n, a in self.aufrufe if n == "penecho_start_session"]

    def flaechen(self, ab: int = 0, bis: int | None = None) -> set[str]:
        """sessionIds aller Aufrufe an eine Flaeche (alles ausser start_session) im Ausschnitt."""
        return {a["sessionId"] for n, a in self.aufrufe[ab:bis] if "sessionId" in a}


def _tab_penecho(tmp_path: Any, mcp: TabMcp) -> PenEchoMcp:
    zustand = tmp_path / "penecho"
    (zustand / "direct-http").mkdir(parents=True)
    (zustand / "direct-http" / "identity.json").write_text(
        json.dumps({"accessToken": "t" * 64, "certificatePem": "unbenutzt-im-mock"}), encoding="utf-8")
    return PenEchoMcp(zustand, transport=httpx.MockTransport(mcp))


def _tab(weiche: Weiche, actor: str, kennung: str | None) -> TestClient:
    browser = _browser(weiche, f"{PERSON_COOKIE}={person_cookie(SCHLUESSEL, actor)}")
    if kennung is not None:
        browser.headers[KOPF] = kennung
    return browser


def test_zwei_personen_eigene_flaeche(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    mcp = TabMcp()
    weiche = _ganz(E_LH, LearnHouse(), [], [], penecho=_tab_penecho(tmp_path, mcp))
    a, b = _tab(weiche, PA, KA), _tab(weiche, PB, KB)
    assert a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    nach_a = len(mcp.aufrufe)
    assert b.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    # Je Person eine Sitzung auf der eigenen Flaeche: start_session je mit der canvasId ihres Tabs.
    starts = mcp.starts()
    assert [s["canvasId"] for s in starts] == [KA, KB]
    assert starts[0]["sessionKey"] != starts[1]["sessionKey"]
    # Die Aufgabe von A ging nur an As Flaeche, die von B nur an Bs.
    assert mcp.flaechen(0, nach_a) == {"pe-" + KA}
    assert mcp.flaechen(nach_a) == {"pe-" + KB}
    assert any(n == "penecho_edit_canvas" and a_["sessionId"] == "pe-" + KB for n, a_ in mcp.aufrufe[nach_a:])
    # Abwechselnd weiter: jede Flaechenarbeit landet auf der Flaeche der Person, die fragt.
    for browser, sid, eigene in ((a, SID_PA, KA), (b, SID_PB, KB), (a, SID_PA, KA)):
        vorher = len(mcp.aufrufe)
        browser.post(f"/lernen/api/sitzungen/{sid}/abgeben", json={"revision": 1})
        assert mcp.flaechen(vorher) == {"pe-" + eigene}
    # B schickt die Kennung von A: B bleibt auf der eigenen Flaeche, As Flaeche bekommt nichts von B.
    b.headers[KOPF] = KA
    vorher = len(mcp.aufrufe)
    b.post(f"/lernen/api/sitzungen/{SID_PB}/abgeben", json={"revision": 1})
    assert mcp.flaechen(vorher) == {"pe-" + KB}
    assert [s["canvasId"] for s in mcp.starts()] == [KA, KB]
    # Die Kennungen sind Zugangsdaten zur Flaeche eines Tabs: nie im Log.
    assert KA[3:] not in caplog.text and KB[3:] not in caplog.text


def test_neuer_tab_uebernimmt_eigene_sitzung(tmp_path) -> None:
    mcp = TabMcp()
    weiche = _ganz(E_LH, LearnHouse(), [], [], penecho=_tab_penecho(tmp_path, mcp))
    alt = _tab(weiche, PA, KA)
    assert alt.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    # Dieselbe Person oeffnet den Kurs in einem neuen Tab: die Huelle bindet ihre Sitzung per documentId
    # an die neue canvasId; die Aufgabe kommt dort wieder ins Bild.
    neu = _tab(weiche, PA, KA2)
    vorher = len(mcp.aufrufe)
    assert neu.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    start = mcp.starts()[-1]
    assert start["canvasId"] == KA2 and start["documentId"] == "doc-" + KA
    assert mcp.flaechen(vorher) == {"pe-" + KA2}
    assert any(n == "penecho_edit_canvas" for n, _ in mcp.aufrufe[vorher:])
    # Ab jetzt laeuft alles fuer diese Sitzung ueber den neuen Tab, nie ueber "den zuletzt verbundenen Browser".
    vorher = len(mcp.aufrufe)
    neu.post(f"/lernen/api/sitzungen/{SID_PA}/abgeben", json={"revision": 1})
    assert mcp.flaechen(vorher) == {"pe-" + KA2}
    assert all("canvasId" in s for s in mcp.starts())
    # Eine Anfrage ohne Kopf (z. B. der Link auf die Zusammenfassung) aendert die Bindung nicht.
    neu.headers.pop(KOPF)
    vorher = len(mcp.aufrufe)
    neu.post(f"/lernen/api/sitzungen/{SID_PA}/abgeben", json={"revision": 1})
    assert mcp.flaechen(vorher) == {"pe-" + KA2}
    assert len(mcp.starts()) == 2


@pytest.mark.parametrize("kopf", [None, "", "lf-" + "A" * 32, "lf-" + "a" * 31, "lf-" + "a" * 33, "x" + KA,
                                  "lf-" + "g" * 32])
def test_ohne_kopf_wie_bisher(tmp_path, caplog: pytest.LogCaptureFixture, kopf: str | None) -> None:
    caplog.set_level("DEBUG")
    mcp = TabMcp()
    weiche = _ganz(E, None, [], [], penecho=_tab_penecho(tmp_path, mcp))
    inhaber = _browser(weiche, f"{COOKIE}={token_erzeugen(SCHLUESSEL)}")  # Telegram-Notweg
    if kopf is not None:
        inhaber.headers[KOPF] = kopf
    r = inhaber.post(f"/lernen/api/kurse/{KURS}/oeffnen")
    assert r.status_code == 200, r.text  # ungueltiger Kopf: kein Fehler, nur wie ohne
    # Genau der Aufruf von bisher: kein canvasId, PenEcho waehlt den Browser wie vor Teil B.
    assert mcp.starts() == [{"title": mcp.starts()[0]["title"], "sessionKey": "learning-huelle:" + SID, "restore": True}]
    assert mcp.flaechen() == {"pe-ohne"}
    if kopf:
        assert kopf not in caplog.text


# ---- Task 6, Fix-Runde 1 ---------------------------------------------------------------------------------------

BELEGT_KOPF = "x-lernflaeche-kennung"


def test_fremde_kennung_ohne_eigene_flaeche_nie_der_alte_weg(tmp_path) -> None:
    """Personenwechsel im selben Tab: sessionStorage behaelt As Kennung, Bs Sitzung war nie an eine Flaeche gebunden.

    Nie der alte Weg (start_session ohne canvasId = zuletzt verbundener Browser): die Antwort ist wie bei nicht
    erreichbarer Flaeche, dazu der Kopf `X-Lernflaeche-Kennung: belegt`. Mit neuer Kennung bekommt B die eigene.
    """
    mcp = TabMcp()
    weiche = _ganz(E_LH, LearnHouse(), [], [], penecho=_tab_penecho(tmp_path, mcp))
    a = _tab(weiche, PA, KA)
    r = a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen")
    assert r.status_code == 200 and r.json()["flaeche"] is True and BELEGT_KOPF not in r.headers
    b = _tab(weiche, PB, KA)
    vorher = len(mcp.aufrufe)
    r = b.post(f"/lernen/api/kurse/{KURS_P}/oeffnen")
    assert r.status_code == 200, r.text
    assert r.json()["flaeche"] is False and r.headers[BELEGT_KOPF] == "belegt"
    r = b.post(f"/lernen/api/sitzungen/{SID_PB}/abgeben", json={"revision": 1})
    assert r.headers[BELEGT_KOPF] == "belegt"
    # Nichts an As Flaeche, nichts an "den zuletzt verbundenen Browser", kein start_session ohne canvasId.
    assert mcp.aufrufe[vorher:] == []
    # Die Seite erneuert die Kennung: jetzt die eigene Flaeche von B.
    b.headers[KOPF] = KB
    r = b.post(f"/lernen/api/kurse/{KURS_P}/oeffnen")
    assert r.status_code == 200 and r.json()["flaeche"] is True and BELEGT_KOPF not in r.headers
    assert mcp.starts()[-1]["canvasId"] == KB and mcp.flaechen(vorher) == {"pe-" + KB}
    assert all("canvasId" in s for s in mcp.starts())


def test_fremde_kennung_bei_gebundener_sitzung_meldet_belegt(tmp_path) -> None:
    mcp = TabMcp()
    weiche = _ganz(E_LH, LearnHouse(), [], [], penecho=_tab_penecho(tmp_path, mcp))
    a, b = _tab(weiche, PA, KA), _tab(weiche, PB, KB)
    assert a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    assert b.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    b.headers[KOPF] = KA
    vorher = len(mcp.aufrufe)
    r = b.post(f"/lernen/api/sitzungen/{SID_PB}/abgeben", json={"revision": 1})
    assert r.headers[BELEGT_KOPF] == "belegt"  # die Seite erneuert die Kennung
    assert mcp.flaechen(vorher) <= {"pe-" + KB}  # B bleibt auf der eigenen Flaeche, nie As


def test_lesende_anfragen_binden_nicht_um(tmp_path) -> None:
    """Tab 1 fragt weiter den Stand ab, Tab 2 hat die Sitzung uebernommen: die Bindung bleibt bei Tab 2."""
    mcp = TabMcp()
    weiche = _ganz(E_LH, LearnHouse(), [], [], penecho=_tab_penecho(tmp_path, mcp))
    alt, neu = _tab(weiche, PA, KA), _tab(weiche, PA, KA2)
    assert alt.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    assert neu.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    alt.get(f"/lernen/api/sitzungen/{SID_PA}/stand")
    alt.post(f"/lernen/api/sitzungen/{SID_PA}/hinweis")
    alt.post(f"/lernen/api/sitzungen/{SID_PA}/erklaeren", json={})
    alt.get(f"/lernen/api/notizen?course_id={KURS_P}")
    ohne = _tab(weiche, PA, None)
    vorher = len(mcp.aufrufe)
    ohne.post(f"/lernen/api/sitzungen/{SID_PA}/abgeben", json={"revision": 1})
    assert mcp.flaechen(vorher) == {"pe-" + KA2}
    # Eine aendernde Anfrage aus Tab 1 holt die Sitzung dagegen zurueck.
    vorher = len(mcp.aufrufe)
    alt.post(f"/lernen/api/sitzungen/{SID_PA}/abgeben", json={"revision": 1})
    assert mcp.flaechen(vorher) == {"pe-" + KA}


def test_personen_cookie_ohne_kopf_wie_bisher(tmp_path) -> None:
    """Alter Tab einer LearnHouse-Person (Seite vor Teil B, ohne Kopf): bisheriges Verhalten."""
    mcp = TabMcp()
    weiche = _ganz(E_LH, LearnHouse(), [], [], penecho=_tab_penecho(tmp_path, mcp))
    a = _tab(weiche, PA, None)
    r = a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen")
    assert r.status_code == 200 and r.json()["flaeche"] is True and BELEGT_KOPF not in r.headers
    assert mcp.starts() == [{"title": mcp.starts()[0]["title"], "sessionKey": "learning-huelle:" + SID_PA,
                             "restore": True}]
    assert mcp.flaechen() == {"pe-ohne"}


# ---- Teil B, Abschluss-Korrektur: Zusammenfassung nur fuer den Inhaber ---------------------------------

def _zf_person(ordner: Any, actor: str, ll: ListenLern, **kw: Any) -> TestClient:
    """Listenkurs KURS als `actor`; die Person sieht ihn ueber LearnHouse unter demselben Titel wie der Inhaber."""
    lh = LearnHouse()
    lh.kurse[PB] = [{"course_id": KURS, "titel": "Testkurs"}]
    einstellung = dataclasses.replace(E_LH, zusammenfassung_ordner=str(ordner))
    client, _, _ = _listen_offen(ll, einstellung=einstellung, katalog=_katalog(lh), actor=actor, **kw)
    return client


def test_zusammenfassung_nur_fuer_den_inhaber_im_wissensordner(tmp_path) -> None:
    ordner = tmp_path / "wissen"
    inhaber = _zf_person(ordner, ACTOR, ListenLern(2))
    try:
        assert inhaber.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
    finally:
        inhaber.app.beenden()
    (datei,) = _md_dateien(ordner)
    vorher = datei.read_bytes()
    # Person B: gleicher Kurs, gleicher Tag, anderer Stand. Das PDF bekommt sie, der Ordner bleibt unberuehrt.
    ll_b = ListenLern(3)
    ll_b.eintraege[0]["status"] = "bestaetigt"
    person_b = _zf_person(ordner, PB, ll_b)
    try:
        r = person_b.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
    finally:
        person_b.app.beenden()
    assert _md_dateien(ordner) == [datei]
    assert datei.read_bytes() == vorher
    assert not list(ordner.glob("*.tmp"))


def test_kursende_einer_person_legt_nichts_ab(tmp_path) -> None:
    ordner = tmp_path / "wissen"
    datei = tmp_path / "kursende.json"
    datei.write_text(json.dumps({f"{PB}|{KURS}": SID}), encoding="utf-8")
    ll = ListenLern(2)
    for e in ll.eintraege:
        e["status"] = "bestaetigt"
    ll.lern.antworten["learning_course_overview"] = _ueberblick(None)
    lh = LearnHouse()
    lh.kurse[PB] = [{"course_id": KURS, "titel": "Testkurs"}]
    client, _, _ = bauen(ll.lern, kursende=datei, katalog=_katalog(lh), actor=PB,
                         einstellung=dataclasses.replace(E_LH, zusammenfassung_ordner=str(ordner)))
    try:
        assert client.post(f"/lernen/api/kurse/{KURS}/oeffnen").json()["abgeschlossen"] is True
        assert not ordner.exists() or _md_dateien(ordner) == []
    finally:
        client.app.beenden()


def test_eigene_dokumente_je_person(tmp_path) -> None:
    # Die Ablage-Pruefung des Durchreichers: jede Person nur die Boards ihrer Lernsitzungen, der Inhaber alle.
    mcp = TabMcp()
    weiche = _ganz(E_LH, LearnHouse(), [], [], penecho=_tab_penecho(tmp_path, mcp))
    a, b = _tab(weiche, PA, KA), _tab(weiche, PB, KB)
    assert a.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    assert b.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
    bruecke = weiche._api
    assert bruecke.eigene_dokumente(PA) == frozenset({"doc-" + KA})
    assert bruecke.eigene_dokumente(PB) == frozenset({"doc-" + KB})
    assert bruecke.eigene_dokumente("lh-1-niemand") == frozenset()
    assert bruecke.eigene_dokumente(ACTOR) is None  # Inhaber: nicht eingeschraenkt


# ---- Teil C, Task 8: Gruppensitzung mit eigener Mappe ------------------------------------------------------

from spaces.learning.huelle.anordnung import gruppen_raster, mappen_bereiche, mappen_kopf, mappen_ursprung  # noqa: E402
from spaces.learning.huelle.bruecke import gruppen_lern_kennung  # noqa: E402
from spaces.learning.huelle.gruppe import (  # noqa: E402
    GRUPPE_VOLL, GRUPPE_WEG, KEIN_MITGLIED, GruppeWeg, KeinMitglied, Mitgliedschaft, person_kennung,
)
from spaces.learning.huelle.katalog import UebersichtWeg  # noqa: E402
from spaces.learning.huelle.penecho_mcp import felder  # noqa: E402

GID = 7
SID_GA = "99999999-9999-4999-8999-999999999991"
SID_GB = "99999999-9999-4999-8999-999999999992"
SID_SA = "99999999-9999-4999-8999-999999999993"
MA = Mitgliedschaft(course_id=KURS_P, gruppe_name="Mathe 9b", anzeigename="Anna A.", offen=True)
MB = Mitgliedschaft(course_id=KURS_P, gruppe_name="Mathe 9b", anzeigename="", offen=True)


class FakeGruppen:
    """Mitgliedschaft wie `gruppe.Gruppen.mitglied`: KeinMitglied / UebersichtWeg."""

    def __init__(self, mitglieder: dict[str, Mitgliedschaft] | None = None) -> None:
        self.mitglieder = dict(mitglieder if mitglieder is not None else {PA: MA, PB: MB})
        self.weg = False
        self.anfragen: list[tuple[str, int]] = []

    def mitglied(self, actor: str, gruppe_id: int, *, frisch: bool = False) -> Mitgliedschaft:
        self.anfragen.append((actor, gruppe_id))
        if self.weg:
            raise UebersichtWeg()
        if gruppe_id != GID or actor not in self.mitglieder:
            raise KeinMitglied()
        return self.mitglieder[actor]


class FakeAbgleich:
    """`/platz` (je Person fest, fortlaufend ab `ab`) und `/schutz`; protokolliert in `log` (gemeinsam mit PenEcho)."""

    def __init__(self, log: list[tuple[Any, ...]], *, ab: int = 0) -> None:
        self.log = log
        self.plaetze: dict[str, int] = {}
        self.ab = ab
        self.platz_weg = False
        self.schutz_weg = False
        self.schutz: list[tuple[str, str, list[list[int]]]] = []

    def platz(self, flaeche: str, person: str) -> int:
        self.log.append(("platz", flaeche, person))
        if self.platz_weg:
            raise GruppeWeg()
        return self.plaetze.setdefault(person, self.ab + len(self.plaetze))

    def schutz_setzen(self, flaeche: str, person: str, felder_: list[list[int]]) -> None:
        self.log.append(("schutz_setzen", flaeche, person))
        if self.schutz_weg:
            raise GruppeWeg()
        self.schutz.append((flaeche, person, felder_))


class Laeufe:
    """Learning mit getrennten Listen-Durchlaeufen je Learning-Kennung (Solo und Gruppe je Person).

    Eine Sitzung gehoert genau einer Kennung; fragt eine andere, lehnt Learning ab (wie `_assert_owned`).
    Der Ueberblick nennt nur die eigene laufende Sitzung der fragenden Kennung (wie `resume`).
    """

    def __init__(self, sids: dict[str, str], *, n: int = 3, gestartet: tuple[str, ...] = ()) -> None:
        self.sids = sids
        self.eintraege = {k: [_eintrag(i) for i in range(1, n + 1)] for k in sids}
        self.gestartet = set(gestartet)
        self.halt = threading.Event()
        self.halt.set()
        self.lern = FakeLern({
            "learning_course_overview": lambda kw: {
                **_ueberblick(self._turn(self._wer()) if self._wer() in self.gestartet else None),
                "course_id": KURS_P},
            "learning_session_start": self._start,
            "learning_task_next": lambda kw: {"session": self._turn(self._besitzer(kw))},
            "learning_task_list": lambda kw: {"session": self._turn(self._besitzer(kw)),
                                              "aufgaben": copy.deepcopy(self.eintraege[self._besitzer(kw)])},
            "learning_task_preview": self._vorbewerten,
        })

    def _wer(self) -> str:
        return self.lern.akteure[-1]

    def _besitzer(self, kw: dict[str, Any]) -> str:
        wer = self._wer()
        if self.sids.get(wer) != kw["session_id"] or wer not in self.gestartet:
            raise LearningAbgelehnt("session belongs to another learner", "session_access_denied")
        return wer

    def _turn(self, wer: str) -> dict[str, Any]:
        t = _listen_turn(self.eintraege[wer])
        return {**t, "session_id": self.sids[wer], "course_id": KURS_P}

    def _start(self, kw: dict[str, Any]) -> dict[str, Any]:
        wer = self._wer()
        self.gestartet.add(wer)
        return {"session": self._turn(wer)}

    def _vorbewerten(self, kw: dict[str, Any]) -> dict[str, Any]:
        wer = self._besitzer(kw)
        assert self.halt.wait(5), "Vorbewertung nie freigegeben"
        (e,) = [e for e in self.eintraege[wer] if e["attempt_id"] == kw["payload"]["attempt_id"]]
        e["vorbewertung"] = _vor(vid=_vid(e["nummer"]), gelesen=str(e["nummer"]))
        return {"session": self._turn(wer), "vorbewertung": e["vorbewertung"]}


def _gruppen_bruecke(laeufe: Laeufe, *, gruppen: FakeGruppen | None = None, abgleich: FakeAbgleich | None = None,
                     penecho: FakePenEcho | None = None, kursende: Any = None, actor: str = PA,
                     kennung: str | None = KA, bewerter: Any = None,
                     ) -> tuple[TestClient, FakePenEcho, FakeAbgleich, FakeGruppen]:
    """`kennung`: Flaechen-Kennung des Tabs (Kopf X-Lernflaeche); die Gruppenflaeche gibt es nur mit ihr."""
    penecho = penecho or FakePenEcho()
    abgleich = abgleich or FakeAbgleich(penecho.aufrufe)
    gruppen = gruppen or FakeGruppen()
    bruecke = Bruecke(E_LH, laeufe.lern, penecho, Sperre(), httpx.Client(transport=httpx.MockTransport(_api_leer)),  # type: ignore[arg-type]
                      kursende, katalog=_katalog(LearnHouse()), gruppen=gruppen, abgleich=abgleich,
                      bewerter=bewerter)
    client = als(TestClient(bruecke), actor)
    if kennung is not None:
        client.headers[KOPF] = kennung
    return client, penecho, abgleich, gruppen


def _schutzfelder(bereiche: list[dict[str, int]]) -> list[list[int]]:
    return [[f["x"], f["y"], f["w"], f["h"]] for f in (felder(b).ergebnisfeld for b in bereiche)]


def test_gruppe_oeffnen_setzt_schild_aufgaben_schutz() -> None:
    laeufe = Laeufe({gruppen_lern_kennung(PA, GID): SID_GA})
    client, penecho, abgleich, _ = _gruppen_bruecke(laeufe)
    abgleich.plaetze["p-jemand-anders"] = 0  # A bekommt Platz 1
    bruecke = client.app
    try:
        r = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 200, r.text
        body = r.json()
        person = person_kennung(PA)
        assert body["gruppe"] == {"flaeche": f"g-{GID}", "person": person, "name": "Anna A.", "gruppe_name": "Mathe 9b"}
        assert body["kurs"] == {"course_id": KURS_P, "titel": "Kurs B"}
        assert body["turn"]["session_id"] == SID_GA and body["stand"]["ablauf"] == "liste"
        assert body["flaeche"] is True and body["abgeschlossen"] is False and body["nummer"] == 1
        # Eigener Learning-Durchlauf unter der Gruppen-Kennung, als feste Liste.
        (start,) = _aufrufe(laeufe.lern, "learning_session_start")
        assert start["course_id"] == KURS_P and start["payload"]["ablauf"] == "liste"
        assert set(laeufe.lern.akteure) == {gruppen_lern_kennung(PA, GID)}
        # Mappe auf Platz 1: Schild im Kopf, erster Bereich bei Ursprung + (0, 150). Neue Gruppe: neues Raster
        # (eine Teilspalte, 100 Luft je Richtung, also Platzbreite 2200).
        raster = gruppen_raster([800] * 3)
        assert (raster.teilspalten, raster.zugabe) == (1, 100)
        ursprung = mappen_ursprung(1, raster)
        assert ursprung == (2300, 100)
        bereiche = mappen_bereiche([800] * 3, ursprung, raster)
        assert bereiche[0] == {"x": 2300, "y": 250, "w": 1300, "h": 800}
        (schild,) = [a for a in penecho.aufrufe if a[0] == "schild_setzen"]
        assert schild[2:4] == (f"mappe-{GID}-{person}", "Anna A.")
        kopf = schild[4]
        assert kopf == mappen_kopf(ursprung)
        assert ursprung[0] <= kopf["x"] and ursprung[1] <= kopf["y"] and kopf["y"] + kopf["h"] <= ursprung[1] + 150
        assert sorted(penecho.rechtecke, key=lambda t: t[0]) == [(1, bereiche[0]), (2, bereiche[1]), (3, bereiche[2])]
        # Schutz mit den Ergebnisfeldern aller Bereiche der Mappe, beim Dienst unter Flaeche und Person.
        assert abgleich.schutz == [(f"g-{GID}", person, _schutzfelder(bereiche))]
        assert ("bereiche_melden", "pe-1", bereiche) in penecho.aufrufe
        assert [a for a in penecho.aufrufe if a[0] == "bereich_zeigen"] == [("bereich_zeigen", "pe-1", bereiche[0])]
        # Reihenfolge: Platz, Schild, Aufgaben, Schutz, Bereiche melden, zeigen. Alles sofort, nichts im Hintergrund.
        namen = [a[0] for a in penecho.aufrufe]
        assert namen.index("platz") < namen.index("schild_setzen") < namen.index("aufgabe_setzen")
        letzte_aufgabe = len(namen) - 1 - namen[::-1].index("aufgabe_setzen")
        assert letzte_aufgabe < namen.index("schutz_setzen") < namen.index("bereiche_melden") < namen.index("bereich_zeigen")
        assert bruecke._flaechenspur.wartend(f"{SID_GA}:setzen:") == 0
    finally:
        bruecke.beenden()


def test_gruppe_zweimal_oeffnen_setzt_einmal(tmp_path) -> None:
    laeufe = Laeufe({gruppen_lern_kennung(PA, GID): SID_GA})
    kursende = tmp_path / "kursende.json"
    client, penecho, abgleich, _ = _gruppen_bruecke(laeufe, kursende=kursende)
    try:
        for _ in range(2):
            assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        namen = penecho.namen()
        assert namen.count("schild_setzen") == 1 and namen.count("aufgabe_setzen") == 3
        assert len(abgleich.schutz) == 1
        assert namen.count("bereich_zeigen") == 2  # zeigen wie im Solo bei jedem Oeffnen
        gemerkt = json.loads((tmp_path / "gruppen-gesetzt.json").read_text(encoding="utf-8"))
        r3 = gruppen_raster([800] * 3)
        assert gemerkt == {f"{PA}|{KURS_P}|g{GID}": {"sid": SID_GA, "gesetzt": True},
                           f"raster|{KURS_P}|g{GID}": {"breite": r3.breite, "hoehe": r3.hoehe,
                                                       "spalten": r3.spalten, "zeilen": r3.zeilen,
                                                       "teilspalten": 1, "zugabe": 100}}
        assert not kursende.exists() or f"{PA}|{KURS_P}" not in json.loads(kursende.read_text(encoding="utf-8"))
    finally:
        client.app.beenden()
    # Nach einem Neustart der Huelle gilt die Datei: wieder nichts doppelt.
    client2, penecho2, abgleich2, _ = _gruppen_bruecke(laeufe, kursende=kursende)
    try:
        # Eine Abgabe in der Gruppensitzung gleich nach dem Neustart (ohne Oeffnen): die Sitzung kommt aus der
        # Datei, Learning wird unter der Gruppen-Kennung gefragt, das Rechteck ist das der Mappe.
        r = client2.post(f"/lernen/api/sitzungen/{SID_GA}/abgeben", json={"nummer": 2})
        assert r.status_code == 202, r.text
        assert set(laeufe.lern.akteure[-3:]) == {gruppen_lern_kennung(PA, GID)}
        bereiche = mappen_bereiche([800] * 3, mappen_ursprung(0, gruppen_raster([800] * 3)))
        assert penecho2.aufnahme_rechtecke == [(2, bereiche[1])]
        assert client2.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        assert "schild_setzen" not in penecho2.namen() and "aufgabe_setzen" not in penecho2.namen()
        assert abgleich2.schutz == []
    finally:
        client2.app.beenden()


def test_gruppe_eigener_durchlauf(tmp_path) -> None:
    gk = gruppen_lern_kennung(PA, GID)
    laeufe = Laeufe({PA: SID_SA, gk: SID_GA}, gestartet=(PA,))
    kursende = tmp_path / "kursende.json"
    client, penecho, _, _ = _gruppen_bruecke(laeufe, kursende=kursende)
    bruecke = client.app
    try:
        solo = client.post(f"/lernen/api/kurse/{KURS_P}/oeffnen")
        assert solo.status_code == 200 and solo.json()["turn"]["session_id"] == SID_SA
        assert "gruppe" not in solo.json()
        vorher = len(laeufe.lern.akteure)
        gruppe = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert gruppe.status_code == 200 and gruppe.json()["turn"]["session_id"] == SID_GA
        # Der Gruppen-Durchlauf startet neu (nicht der Solo-Durchlauf) und spricht nur unter der Gruppen-Kennung.
        assert _aufrufe(laeufe.lern, "learning_session_start") and set(laeufe.lern.akteure[vorher:]) == {gk}
        # Solo erneut: dieselbe Solo-Sitzung, die Gruppensitzung bleibt gemerkt.
        vorher = len(laeufe.lern.akteure)
        solo2 = client.post(f"/lernen/api/kurse/{KURS_P}/oeffnen")
        assert solo2.json()["turn"]["session_id"] == SID_SA and set(laeufe.lern.akteure[vorher:]) == {PA}
        assert {SID_SA, SID_GA} <= set(bruecke._sitzungen)
        assert bruecke._sitzungen[SID_SA].gruppe is None and bruecke._sitzungen[SID_GA].gruppe == GID
        # Jede Sitzung arbeitet in ihrem eigenen Raster: Solo bei (100, 100), die Gruppe in der Mappe.
        assert client.post(f"/lernen/api/sitzungen/{SID_SA}/abgeben", json={"nummer": 1}).status_code == 202
        assert client.post(f"/lernen/api/sitzungen/{SID_GA}/abgeben", json={"nummer": 1}).status_code == 202
        gruppe_bereiche = mappen_bereiche([800] * 3, mappen_ursprung(0, gruppen_raster([800] * 3)))
        assert penecho.aufnahme_rechtecke == [(1, anordnung([False] * 3)[0]), (1, gruppe_bereiche[0])]
        _bis(lambda: bruecke._modellspur.wartend("") == 0)
        akteure = dict(zip((kw.get("session_id") for _, kw in laeufe.lern.aufrufe), laeufe.lern.akteure))
        assert akteure[SID_SA] == PA and akteure[SID_GA] == gk
    finally:
        bruecke.beenden()


def test_zwei_personen_geben_gleichzeitig_ab() -> None:
    ga, gb = gruppen_lern_kennung(PA, GID), gruppen_lern_kennung(PB, GID)
    laeufe = Laeufe({ga: SID_GA, gb: SID_GB})
    a, penecho, abgleich, _ = _gruppen_bruecke(laeufe)
    b = als(TestClient(a.app), PB)
    b.headers[KOPF] = KB
    bruecke = a.app
    try:
        assert a.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        rb = b.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert rb.status_code == 200 and rb.json()["gruppe"]["name"] == "Teilnehmende Person"
        assert rb.json()["gruppe"]["person"] == person_kennung(PB) != person_kennung(PA)
        schilder = [a_[3] for a_ in penecho.aufrufe if a_[0] == "schild_setzen"]
        assert schilder == ["Anna A.", "Teilnehmende Person"]  # nie die Kennung oder eine Adresse
        laeufe.halt.clear()
        ra = a.post(f"/lernen/api/sitzungen/{SID_GA}/abgeben", json={"nummer": 1})
        rb = b.post(f"/lernen/api/sitzungen/{SID_GB}/abgeben", json={"nummer": 1})
        assert ra.status_code == 202 and rb.status_code == 202, (ra.text, rb.text)
        # Beide Vorschauen liegen in der Spur, keine blockiert die andere Person.
        assert bruecke._modellspur.wartend(f"{SID_GA}:abgabe:") == 1
        assert bruecke._modellspur.wartend(f"{SID_GB}:abgabe:") == 1
        raster = gruppen_raster([800] * 3)
        bereiche_a = mappen_bereiche([800] * 3, mappen_ursprung(0, raster))
        bereiche_b = mappen_bereiche([800] * 3, mappen_ursprung(1, raster))
        assert penecho.aufnahme_rechtecke == [(1, bereiche_a[0]), (1, bereiche_b[0])]
        laeufe.halt.set()
        _bis(lambda: bruecke._modellspur.wartend("") == 0 and bruecke._flaechenspur.wartend("") == 0)
        vorschauen = [akt for (tool, _), akt in zip(laeufe.lern.aufrufe, laeufe.lern.akteure)
                      if tool == "learning_task_preview"]
        assert sorted(vorschauen) == sorted([ga, gb])
        # Die Karten landen je in der eigenen Mappe.
        assert sorted(penecho.karten_rechtecke, key=lambda k: k[1]["x"]) == [(1, bereiche_a[0]), (1, bereiche_b[0])]
        # Jede Person sieht nur ihre Sitzung: B kommt nicht an die Gruppensitzung von A.
        assert b.get(f"/lernen/api/sitzungen/{SID_GA}/stand").status_code == 404
    finally:
        laeufe.halt.set()
        bruecke.beenden()


def test_gruppe_dienst_weg_meldung_solo_geht() -> None:
    gk = gruppen_lern_kennung(PA, GID)
    laeufe = Laeufe({PA: SID_SA, gk: SID_GA}, gestartet=(PA,))
    client, penecho, abgleich, _ = _gruppen_bruecke(laeufe)
    try:
        abgleich.platz_weg = True
        r = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 503 and r.json() == {"meldung": GRUPPE_WEG}
        assert gk not in laeufe.lern.akteure  # ohne Platz kein Durchlauf
        # Solo haengt nicht vom Abgleichdienst ab.
        assert client.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
        # Schutz scheitert nach dem Setzen: 503, die Mappe gilt nicht als gesetzt; der naechste Versuch schickt alles.
        abgleich.platz_weg, abgleich.schutz_weg = False, True
        r = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 503 and r.json() == {"meldung": GRUPPE_WEG}
        assert penecho.namen().count("aufgabe_setzen") >= 3
        abgleich.schutz_weg = False
        vorher = penecho.namen().count("schild_setzen")
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        assert penecho.namen().count("schild_setzen") == vorher + 1 and len(abgleich.schutz) == 1
        # Voll: der Platz liegt ausserhalb des Rasters.
        voll = FakeAbgleich([], ab=10_000)
        client2, _, _, _ = _gruppen_bruecke(Laeufe({gk: SID_GA}), abgleich=voll)
        r = client2.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 409 and r.json() == {"meldung": GRUPPE_VOLL}
        client2.app.beenden()
    finally:
        client.app.beenden()


def test_kein_mitglied_403() -> None:
    gk = gruppen_lern_kennung(PA, GID)
    laeufe = Laeufe({gk: SID_GA})
    gruppen = FakeGruppen({PB: MB})
    client, penecho, abgleich, _ = _gruppen_bruecke(laeufe, gruppen=gruppen)
    try:
        r = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 403 and r.json() == {"meldung": KEIN_MITGLIED}
        assert laeufe.lern.aufrufe == [] and penecho.aufrufe == []  # weder Learning noch Platz
        gruppen.weg = True
        r = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 503 and r.json() == {"meldung": GRUPPE_WEG}
        for falsch in ("0", "abc", "1234567890", "-1", "07"):
            assert client.post(f"/lernen/api/gruppen/{falsch}/oeffnen").status_code == 404, falsch
        # Mitglied, dann entfernt: die Gruppensitzung antwortet danach 403.
        gruppen.weg = False
        gruppen.mitglieder[PA] = MA
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        del gruppen.mitglieder[PA]
        vorher = len(laeufe.lern.aufrufe)
        r = client.get(f"/lernen/api/sitzungen/{SID_GA}/stand")
        assert r.status_code == 403 and r.json() == {"meldung": KEIN_MITGLIED}
        assert len(laeufe.lern.aufrufe) == vorher
    finally:
        client.app.beenden()


def test_ohne_gruppen_einstellung_404() -> None:
    laeufe = Laeufe({PA: SID_SA}, gestartet=(PA,))
    client, _, _ = bauen(laeufe.lern, einstellung=E_LH, katalog=_katalog(LearnHouse()), actor=PA)
    try:
        r = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 404 and r.json() == {"meldung": GRUPPE_WEG}
        assert laeufe.lern.aufrufe == []
    finally:
        client.app.beenden()


def test_gruppe_bindet_den_tab_mit_gruppe_solo_unveraendert() -> None:
    """Die Gruppensitzung startet PenEcho im Tab der Person mit ihrer Gruppe (target "current" in PenEchoMcp);
    die Solo-Sitzung derselben Person ruft `sitzung` genau wie bisher, ohne Gruppe."""
    gk = gruppen_lern_kennung(PA, GID)
    laeufe = Laeufe({PA: SID_SA, gk: SID_GA}, gestartet=(PA,))
    client, penecho, _, _ = _gruppen_bruecke(laeufe)
    try:
        assert client.post(f"/lernen/api/kurse/{KURS_P}/oeffnen").status_code == 200
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        sitzungen = [a for a in penecho.aufrufe if a[0] == "sitzung"]
        assert {a[1:] for a in sitzungen if a[1] == SID_SA} == {(SID_SA, sitzungen[0][2], KA)}
        gruppe = [a for a in sitzungen if a[1] == SID_GA]
        assert gruppe and all(len(a) == 5 and a[3] == KA and a[4] == f"g-{GID}" for a in gruppe)
    finally:
        client.app.beenden()


def test_gruppe_ohne_flaechen_kennung_nie_im_hintergrund() -> None:
    """Ohne Kopf X-Lernflaeche gibt es fuer die Gruppe keine Flaeche (nie der zuletzt verbundene Browser oder eine
    unsichtbare Hintergrundflaeche): flaeche false, nichts gesetzt, kein Schutz; mit Kennung danach alles."""
    from spaces.learning.huelle.penecho_mcp import PenEchoGruppenbindung

    laeufe = Laeufe({gruppen_lern_kennung(PA, GID): SID_GA})
    client, penecho, abgleich, _ = _gruppen_bruecke(laeufe, kennung=None)
    try:
        r = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 200 and r.json()["flaeche"] is False
        assert [a for a in penecho.aufrufe if a[0] in ("sitzung", "schild_setzen", "aufgabe_setzen")] == []
        assert abgleich.schutz == []
        # PenEcho lehnt die Bindung ab (GROUP_CANVAS_ONLY/BINDING_CONFLICT): ebenso nichts gesetzt, kein Neuversuch.
        client.headers[KOPF] = KA
        penecho.fehler = PenEchoGruppenbindung("GROUP_CANVAS_ONLY")
        r = client.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 200 and r.json()["flaeche"] is False and abgleich.schutz == []
        assert "vergessen" not in penecho.namen()
        penecho.fehler = None
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").json()["flaeche"] is True
        assert len(abgleich.schutz) == 1
    finally:
        client.app.beenden()


# ---- Teil C, Task 8, Fix-Runde 2: Bilder vor der Sperre, festes Raster je Gruppe, Bericht ohne Bilder ---------------

def test_gruppe_laedt_aufgabenbilder_vor_der_flaechensperre() -> None:
    """Alle Bilder der Mappe kommen vor der Flaechensperre (wie Solo `_liste_oeffnen`); gesetzt wird danach."""
    laeufe = Laeufe({gruppen_lern_kennung(PA, GID): SID_GA})
    client, penecho, _, _ = _gruppen_bruecke(laeufe)
    bruecke = client.app
    echt = bruecke._aufgabenbilder

    def bilder(task: dict[str, Any]) -> list[Any]:
        assert not bruecke._flaeche_sperre._is_owned(), "Bild unter der Flaechensperre geladen"
        penecho.aufrufe.append(("bilder", task["id"]))
        return echt(task)

    bruecke._aufgabenbilder = bilder
    try:
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        namen = penecho.namen()
        assert namen.count("bilder") == 3 and namen.count("aufgabe_setzen") == 3
        assert max(i for i, n in enumerate(namen) if n == "bilder") < namen.index("schild_setzen")
        # Ist die Mappe gesetzt, laedt ein erneutes Oeffnen keine Bilder.
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        assert penecho.namen().count("bilder") == 3
    finally:
        bruecke.beenden()


def test_gruppe_raster_fest_je_gruppe(tmp_path) -> None:
    """Die erste gesetzte Mappe legt das Raster der Gruppe fest (gruppen-gesetzt.json); spaeter Kommende nutzen es.
    Passt eine Mappe nicht in die feste Zelle: 409 GRUPPE_VOLL, bevor irgendetwas gesetzt wird."""
    ga, gb = gruppen_lern_kennung(PA, GID), gruppen_lern_kennung(PB, GID)
    laeufe = Laeufe({ga: SID_GA, gb: SID_GB})
    laeufe.eintraege[gb] = [_eintrag(i, typ="penecho_canvas") for i in range(1, 4)]  # hoeher (Revision)
    kursende = tmp_path / "kursende.json"
    a, penecho, abgleich, _ = _gruppen_bruecke(laeufe, kursende=kursende)
    b = als(TestClient(a.app), PB)
    b.headers[KOPF] = KB
    try:
        assert a.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        fest = gruppen_raster([800] * 3)
        gemerkt = json.loads((tmp_path / "gruppen-gesetzt.json").read_text(encoding="utf-8"))
        assert gemerkt[f"raster|{KURS_P}|g{GID}"] == {
            "breite": fest.breite, "hoehe": fest.hoehe, "spalten": fest.spalten, "zeilen": fest.zeilen,
            "teilspalten": fest.teilspalten, "zugabe": fest.zugabe}
        vorher = len(penecho.aufrufe)
        r = b.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 409 and r.json() == {"meldung": GRUPPE_VOLL}
        assert [x for x in penecho.aufrufe[vorher:] if x[0] in ("schild_setzen", "aufgabe_setzen")] == []
        assert len(abgleich.schutz) == 1
        # Gleiche Hoehen: das feste Raster (Platz 1 im festen Raster).
        laeufe.eintraege[gb] = [_eintrag(i) for i in range(1, 4)]
        assert b.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        bereiche_b = mappen_bereiche([800] * 3, mappen_ursprung(1, fest))
        assert abgleich.schutz[-1] == (f"g-{GID}", person_kennung(PB), _schutzfelder(bereiche_b))
    finally:
        a.app.beenden()
    # Nach einem Neustart gilt das feste Raster weiter (aus gruppen-gesetzt.json). Bs Mappe ist schon gesetzt: auch
    # mit jetzt hoeheren Aufgaben nie 409, die Abgabe rechnet im festen Raster (Platz 1 laut Dienst).
    laeufe.eintraege[gb] = [_eintrag(i, typ="penecho_canvas") for i in range(1, 4)]
    dienst = FakeAbgleich([])
    dienst.plaetze = {person_kennung(PA): 0, person_kennung(PB): 1}
    c, penecho2, _, _ = _gruppen_bruecke(laeufe, kursende=kursende, actor=PB, kennung=KB, abgleich=dienst)
    try:
        r = c.post(f"/lernen/api/sitzungen/{SID_GB}/abgeben", json={"nummer": 1})
        assert r.status_code == 202, r.text
        assert penecho2.aufnahme_rechtecke == [(1, mappen_bereiche([1100] * 3, mappen_ursprung(1, fest))[0])]
        assert c.get(f"/lernen/api/sitzungen/{SID_GB}/stand").status_code == 200
    finally:
        c.app.beenden()
    # Wer noch keine Mappe hat und hoeher ist, bekommt beim Oeffnen weiter 409 (auch nach dem Neustart).
    laeufe2 = Laeufe({gruppen_lern_kennung(PA, GID): SID_GA, gb: SID_GB})
    laeufe2.eintraege[gb] = [_eintrag(i, typ="penecho_canvas") for i in range(1, 4)]
    leer = tmp_path / "neu" / "kursende.json"
    leer.parent.mkdir()
    (leer.parent / "gruppen-gesetzt.json").write_text(json.dumps(
        {k: v for k, v in json.loads((tmp_path / "gruppen-gesetzt.json").read_text(encoding="utf-8")).items()
         if not k.startswith(PB + "|")}), encoding="utf-8")
    d, penecho3, _, _ = _gruppen_bruecke(laeufe2, kursende=leer, actor=PB, kennung=KB)
    try:
        r = d.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 409 and r.json() == {"meldung": GRUPPE_VOLL}
        assert [x for x in penecho3.aufrufe if x[0] in ("schild_setzen", "aufgabe_setzen")] == []
    finally:
        d.app.beenden()


def _getrennt_r(a: dict[str, int], b: dict[str, int]) -> bool:
    return (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
            or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


def test_gruppen_abschluss_fest_in_der_eigenen_mappe_solo_unveraendert() -> None:
    """Final B, H3: in der Gruppe steht die Abschlusskarte an einem festen Platz im Kopf der eigenen Mappe, rechts
    neben dem Namensschild (kein Widget, das die Ansicht der Person platziert); Solo wie bisher (Karte + Bilder)."""
    from spaces.learning.huelle.anordnung import KOPF, mappen_abschluss, zeile
    from spaces.learning.huelle.bruecke import _Sitzung
    from spaces.learning.huelle.einstellung import Kurs

    gk = gruppen_lern_kennung(PA, GID)
    laeufe = Laeufe({gk: SID_GA}, gestartet=(gk,))
    client, penecho, _, _ = _gruppen_bruecke(laeufe)
    bruecke = client.app
    kurs = Kurs(course_id=KURS_P, titel="Kurs B")
    fertig = {"state": "completed", "summary": BERICHT, "ablauf": "liste"}
    try:
        solo = _Sitzung(kurs, SID_SA, PA, liste=True)
        bruecke._bericht(solo, fertig, {n: anordnung([False] * 3)[n - 1] for n in (1, 2, 3)})
        gruppe = _Sitzung(kurs, SID_GA, PA, liste=True, gruppe=GID, platz=1, flaeche=KA)
        for _ in range(2):  # hoechstens einmal je Sitzung
            bruecke._bericht(gruppe, fertig, bruecke._bericht_rechtecke(gruppe, fertig))
        (b_solo,) = [a for a in penecho.aufrufe if a[0] == "bericht_zeigen"]
        assert b_solo[4] == (1, 2, 3) and penecho.bericht_rechtecke[0] is not None
        (karte,) = [a for a in penecho.aufrufe if a[0] == "schild_setzen"]
        _, board, kennung, text, platz = karte
        assert board == "pe-1" and kennung == f"abschluss-{SID_GA}" and gruppe.bericht_gezeigt
        assert text == "Kurs abgeschlossen: Kurs B\n1 von 3 Aufgaben richtig."
        raster = gruppen_raster([800] * 3)
        ursprung = mappen_ursprung(1, raster)
        assert platz == mappen_abschluss(ursprung)
        # Innerhalb der Mappe (im Kopf), weder auf dem Namensschild noch in einer Aufgabenzeile.
        ux, uy = ursprung
        assert ux <= platz["x"] and platz["x"] + platz["w"] <= ux + raster.breite
        assert uy <= platz["y"] and platz["y"] + platz["h"] <= uy + KOPF
        assert _getrennt_r(platz, mappen_kopf(ursprung))
        assert all(_getrennt_r(platz, zeile(b)) for b in mappen_bereiche([800] * 3, ursprung))
        # Drei Zeilen zu je 27 px passen in die 110 hohe Flaeche, auch bei langem Kurstitel.
        assert platz["h"] >= 3 * 27
    finally:
        bruecke.beenden()


def test_abschluss_text_kurz_und_ohne_markdown() -> None:
    from spaces.learning.huelle.bericht import abschluss_text

    assert abschluss_text("Kurs B", BERICHT) == "Kurs abgeschlossen: Kurs B\n1 von 3 Aufgaben richtig."
    assert abschluss_text("Kurs  *B*\n", {"results": []}) == "Kurs abgeschlossen: Kurs *B*"
    lang = abschluss_text("Analysis " * 20, BERICHT)
    erste = lang.splitlines()[0]
    assert len(erste) <= 64 and erste.endswith("…") and lang.count("\n") == 1


# ---- Final B, H2: das Schutzfeld ist das blau umrandete Loesungsfeld ------------------------------------------

def _als_liste(r: dict[str, int]) -> list[int]:
    return [r["x"], r["y"], r["w"], r["h"]]


def test_gruppe_schutz_gilt_dem_blauen_feld_je_typ() -> None:
    """Deterministische Typen: Ergebnisfeld; flaechenbewertete Typen (open, case, penecho_canvas): das Arbeitsfeld,
    in das sie laut Hinweis schreiben; code: kein Schutzfeld (kein Rahmen, kein unsichtbares Stueck)."""
    from spaces.learning.huelle.penecho_mcp import loesungsfeld

    gk = gruppen_lern_kennung(PA, GID)
    laeufe = Laeufe({gk: SID_GA})
    typen = ["numeric", "open", "case", "penecho_canvas", "code", "single_choice"]
    laeufe.eintraege[gk] = [_eintrag(n, typ=t) for n, t in enumerate(typen, start=1)]
    client, penecho, abgleich, _ = _gruppen_bruecke(laeufe)
    try:
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        rechteck = dict(penecho.rechtecke)
        f = {n: felder(rechteck[n]) for n in rechteck}
        ((_, _, schutz),) = abgleich.schutz
        assert schutz == [_als_liste(f[1].ergebnisfeld), _als_liste(f[2].arbeitsfeld), _als_liste(f[3].arbeitsfeld),
                          _als_liste(f[4].arbeitsfeld), _als_liste(f[6].ergebnisfeld)]
        # Dieselbe Quelle wie der sichtbare Rahmen: je gesetzter Aufgabe das Loesungsfeld zu ihren Rahmen-Schaltern.
        rahmen = [loesungsfeld(r, mit_ergebnisfeld=e, mit_arbeitsfeld=a) for (_, r), (_, _, e), (_, _, a)
                  in zip(penecho.rechtecke, penecho.ergebnisfelder, penecho.arbeitsfelder)]
        assert rahmen[4] is None  # code
        assert [_als_liste(x) for x in rahmen if x is not None] == schutz
    finally:
        client.app.beenden()


# ---- Gerechte KI (Task 9, Fix): Gruppensitzungen reihen mit der echten Person ein -------------------------------

def test_gruppe_reiht_mit_der_echten_person_ein() -> None:
    gk = gruppen_lern_kennung(PA, GID)
    laeufe = Laeufe({gk: SID_GA})
    client, _, _, _ = _gruppen_bruecke(laeufe)
    bruecke = client.app
    protokoll: list[tuple[str, str, str | None]] = []

    def beobachten(name: str, spur: Any) -> None:
        echt = spur.einreihen

        def einreihen(schluessel: str, arbeit: Any, **kw: Any) -> bool:
            protokoll.append((name, schluessel, kw.get("wer")))
            return echt(schluessel, arbeit, **kw)

        spur.einreihen = einreihen

    beobachten("modell", bruecke._modellspur)
    beobachten("flaeche", bruecke._flaechenspur)
    try:
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        assert client.post(f"/lernen/api/sitzungen/{SID_GA}/abgeben", json={"nummer": 1}).status_code == 202
        _bis(lambda: bruecke._modellspur.wartend("") == 0 and bruecke._flaechenspur.wartend("") == 0)
        assert bruecke._sitzungen[SID_GA].lern_actor == gk != PA
        modell = [p for p in protokoll if p[0] == "modell"]
        flaeche = [p for p in protokoll if p[0] == "flaeche"]
        assert modell and flaeche  # Bewertung und Rueckmeldung
        assert {w for _, _, w in modell + flaeche} == {PA}  # nie `actor:g<gid>`
    finally:
        bruecke.beenden()


# ---- Kapazitaet (12.11.): neues Raster fuer neue Gruppen, alte feste Raster unveraendert ------------------------

def _raster_vektor(fall: str) -> dict[str, Any]:
    from pathlib import Path

    vektor = Path(__file__).with_name("raster_alt_vektor.json")
    return json.loads(vektor.read_text(encoding="utf-8"))[fall]


def test_altes_festes_raster_aus_der_datei_bleibt_exakt(tmp_path) -> None:
    """PFLICHT: eine laufende Gruppe mit altem festen Raster (vier Zahlen in gruppen-gesetzt.json) setzt die Mappe
    einer neuen Person exakt dort, wo sie vor der Kapazitaetsaenderung lag; der Eintrag bleibt bei vier Zahlen."""
    alt = _raster_vektor("3x800")
    kursende = tmp_path / "kursende.json"
    raster_schluessel = f"raster|{KURS_P}|g{GID}"
    (tmp_path / "gruppen-gesetzt.json").write_text(json.dumps({
        f"{PB}|{KURS_P}|g{GID}": {"sid": SID_GB, "gesetzt": True},
        raster_schluessel: alt["raster"]}), encoding="utf-8")
    laeufe = Laeufe({gruppen_lern_kennung(PA, GID): SID_GA})
    client, penecho, abgleich, _ = _gruppen_bruecke(laeufe, kursende=kursende)
    abgleich.plaetze["p-jemand-anders"] = 0  # A bekommt Platz 1
    try:
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        erwartet = alt["plaetze"][1]
        assert erwartet["ursprung"] == [2400, 100]  # altes Raster: Platzbreite 2300
        bereiche = [[b["x"], b["y"], b["w"], b["h"]] for _, b in sorted(penecho.rechtecke, key=lambda t: t[0])]
        assert bereiche == erwartet["bereiche"]
        (schild,) = [a for a in penecho.aufrufe if a[0] == "schild_setzen"]
        assert [schild[4][k] for k in "xywh"] == erwartet["kopf"]
        gemerkt = json.loads((tmp_path / "gruppen-gesetzt.json").read_text(encoding="utf-8"))
        assert gemerkt[raster_schluessel] == alt["raster"]  # vier Zahlen, kein neues Raster
        assert gemerkt[f"{PA}|{KURS_P}|g{GID}"] == {"sid": SID_GA, "gesetzt": True}
    finally:
        client.app.beenden()


def test_raster_eintraege_lesen_alt_neu_kaputt() -> None:
    from spaces.learning.huelle.anordnung import GruppenRaster
    from spaces.learning.huelle.bruecke import _raster_aus, _raster_eintrag

    alt = {"breite": 2100, "hoehe": 2850, "spalten": 8, "zeilen": 6}
    assert _raster_aus(alt) == GruppenRaster(2100, 2850, 8, 6, teilspalten=None, zugabe=200)
    neu = {**alt, "breite": 8850, "hoehe": 3800, "spalten": 2, "zeilen": 5, "teilspalten": 4, "zugabe": 100}
    assert _raster_aus(neu) == GruppenRaster(8850, 3800, 2, 5, teilspalten=4, zugabe=100)
    assert _raster_eintrag(_raster_aus(alt)) == alt and _raster_eintrag(_raster_aus(neu)) == neu
    for kaputt in ({**alt, "teilspalten": 0, "zugabe": 100}, {**alt, "teilspalten": 2}, {**alt, "zugabe": 100},
                   {**alt, "teilspalten": True, "zugabe": 100}, {**alt, "teilspalten": 2, "zugabe": -1},
                   {**alt, "spalten": 0}, {**alt, "teilspalten": None, "zugabe": 200}):
        assert _raster_aus(kaputt) is None, kaputt


def test_neue_gruppe_16_aufgaben_zehn_mappen_dann_voll(tmp_path) -> None:
    """Neue Gruppe mit 16 Aufgaben: vier Teilspalten, zehn Plaetze; Platz 9 bekommt seine Mappe (in vier Teilspalten,
    innerhalb der Flaeche), Platz 10 bekommt 409 GRUPPE_VOLL, bevor etwas gesetzt wird."""
    fest = gruppen_raster([800] * 16)
    assert (fest.teilspalten, fest.plaetze) == (4, 10)
    laeufe = Laeufe({gruppen_lern_kennung(PA, GID): SID_GA}, n=16)
    client, penecho, abgleich, _ = _gruppen_bruecke(laeufe, abgleich=None, kursende=tmp_path / "kursende.json")
    abgleich.ab = 9
    try:
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        bereiche = [b for _, b in sorted(penecho.rechtecke, key=lambda t: t[0])]
        u = mappen_ursprung(9, fest)
        assert bereiche == mappen_bereiche([800] * 16, u, fest)
        assert {b["x"] - u[0] for b in bereiche} == {0, 2250, 4500, 6750}
        assert all(b["x"] + 2100 <= 20_000 and b["y"] + b["h"] <= 20_000 for b in bereiche)
        gemerkt = json.loads((tmp_path / "gruppen-gesetzt.json").read_text(encoding="utf-8"))
        assert gemerkt[f"raster|{KURS_P}|g{GID}"]["teilspalten"] == 4
    finally:
        client.app.beenden()
    laeufe_b = Laeufe({gruppen_lern_kennung(PB, GID): SID_GB}, n=16)
    dienst = FakeAbgleich([], ab=10)
    b, penecho_b, _, _ = _gruppen_bruecke(laeufe_b, abgleich=dienst, kursende=tmp_path / "kursende.json", actor=PB,
                                          kennung=KB)
    try:
        r = b.post(f"/lernen/api/gruppen/{GID}/oeffnen")
        assert r.status_code == 409 and r.json() == {"meldung": GRUPPE_VOLL}
        assert [x for x in penecho_b.aufrufe if x[0] in ("schild_setzen", "aufgabe_setzen")] == []
    finally:
        b.app.beenden()


# ---- KI-Tutor Teil 2, Task 3: Abgabe mit der Bewertung der Flaechen-KI, Rueckfall auf den Server -------------------

GEHEIM = "MUSTERLOESUNG-GEHEIM"
BEWERTUNGSDATEN = {"attempt_id": "77777777-7777-4777-8777-777777777771", "item_type": "numeric", "art": "deterministic",
                   "aufgabe": "Frage", "musterloesung": GEHEIM,
                   "kriterien": [{"criterion_id": "c1", "description": "GEHEIMES-KRITERIUM", "points": 10}],
                   "antwortformat": {"roh": {"wert": "the number as written"},
                                     "etiketten": {"optionen": {}, "links": {}, "rechts": {}}}}
EXTERN = {"grader": "ki_flaeche", "gelesen": {"lesbar": True, "text": "-1,5", "roh": {"wert": "-1,5"}}}


class FakeBewerter:
    """Ersatz fuer KiBewerter: haelt fest, was die Huelle ihm gibt; `antwort` None = Flaechen-KI weg."""

    def __init__(self, antwort: dict[str, Any] | None = None, *, aktiv: bool = True,
                 ereignisse: list[str] | None = None) -> None:
        self.antwort = antwort
        self.aktiv = aktiv
        self.aufrufe: list[tuple[bytes, dict[str, Any]]] = []
        self.ereignisse = ereignisse if ereignisse is not None else []

    def bewerten(self, bild_png: bytes, daten: dict[str, Any]) -> dict[str, Any] | None:
        self.ereignisse.append("bewerter")
        self.aufrufe.append((bild_png, daten))
        return self.antwort


def _ki_sitzung(typ: str = "numeric", *, bewerter: Any, daten: Any = None, vorschau: Any = None):
    offen = turn(TASK_1, typ=typ, mode="training")
    ereignisse: list[str] = bewerter.ereignisse if bewerter is not None else []

    def protokoll(name: str, wert: Any):
        def antwort(kw: dict[str, Any]) -> Any:
            ereignisse.append(name)
            if isinstance(wert, list):
                eintrag = wert.pop(0) if len(wert) > 1 else wert[0]
            else:
                eintrag = wert
            if isinstance(eintrag, Exception):
                raise eintrag
            return eintrag(kw) if callable(eintrag) else eintrag
        return antwort

    lern = FakeLern({
        "learning_course_overview": _ueberblick(offen),
        "learning_task_next": {"session": offen},
        "learning_task_bewertungsdaten": protokoll("bewertungsdaten", BEWERTUNGSDATEN if daten is None else daten),
        "learning_task_preview": protokoll(
            "preview", {"session": offen, "vorbewertung": _vor()} if vorschau is None else vorschau),
    })
    client, sperre, penecho, _ = _offen(lern, None, bewerter=bewerter)
    return lern, client, penecho, ereignisse


def test_abgabe_mit_ki_bewertung() -> None:
    bewerter = FakeBewerter(EXTERN)
    lern, client, penecho, ereignisse = _ki_sitzung("numeric", bewerter=bewerter)
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 200, r.text
    # Erst Bewertungsdaten (lesend), dann die Flaechen-KI, dann die Vorbewertung mit der externen Bewertung.
    assert ereignisse == ["bewertungsdaten", "bewerter", "preview"]
    (daten_kw,) = _aufrufe(lern, "learning_task_bewertungsdaten")
    assert daten_kw["session_id"] == SID and daten_kw["expected_revision"] == 1
    assert daten_kw["payload"] == {} and not daten_kw.get("schreibend")
    assert bewerter.aufrufe == [(PNG, BEWERTUNGSDATEN)]
    (kw,) = _aufrufe(lern, "learning_task_preview")
    assert kw["payload"]["externe_bewertung"] == EXTERN
    assert base64.b64decode(kw["payload"]["handschrift_png_base64"]) == PNG
    # Die Rueckmeldungskarte erscheint wie bisher.
    (karte,) = _karten(penecho)
    assert karte[:6] == ("rueckmeldung_zeigen", "pe-1", TASK_1, 1, "-1,5", "Richtig.")
    assert r.json()["vorbewertung"]["id"] == VOR_ID


def test_abgabe_rubrik_nimmt_den_ganzen_bereich_auch_fuer_die_ki() -> None:
    bewerter = FakeBewerter({"grader": "ki_flaeche", "kriterien": [], "rueckmeldung": "x"})
    _, client, penecho, _ = _ki_sitzung("open", bewerter=bewerter)
    assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1}).status_code == 200
    assert "schnappschuss" in penecho.namen() and "ergebnis_schnappschuss" not in penecho.namen()
    assert bewerter.aufrufe[0][0] == PNG


@pytest.mark.parametrize("fall", ["ki_none", "learning_lehnt_ab", "bewertungsdaten_scheitern",
                                  "bewertungsdaten_abgelehnt"])
def test_abgabe_ruecklauf_auf_server(fall: str, caplog: pytest.LogCaptureFixture) -> None:
    ablehnung = LearningAbgelehnt("external grade invalid", "invalid_session_request")
    bewerter = FakeBewerter(None if fall == "ki_none" else EXTERN)
    daten = (LearningWeg("learning_task_bewertungsdaten") if fall == "bewertungsdaten_scheitern"
             else LearningAbgelehnt("exam", "session_access_denied") if fall == "bewertungsdaten_abgelehnt" else None)
    offen = turn(TASK_1, typ="numeric", mode="training")
    gut = {"session": offen, "vorbewertung": _vor()}
    vorschau = None
    if fall == "learning_lehnt_ab":
        vorschau = [ablehnung, gut]
    lern, client, penecho, _ = _ki_sitzung("numeric", bewerter=bewerter, daten=daten, vorschau=vorschau)
    with caplog.at_level("WARNING"):
        r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 200, r.text
    vorschauen = _aufrufe(lern, "learning_task_preview")
    # Der letzte (erfolgreiche) Aufruf geht immer ohne externe Bewertung an den Server.
    assert "externe_bewertung" not in vorschauen[-1]["payload"]
    assert base64.b64decode(vorschauen[-1]["payload"]["handschrift_png_base64"]) == PNG
    if fall == "learning_lehnt_ab":
        assert len(vorschauen) == 2 and "externe_bewertung" in vorschauen[0]["payload"]
        # Der Rueckfall nennt den Fehlercode, nie den Inhalt der Ablehnung.
        assert "invalid_session_request" in caplog.text and "external grade invalid" not in caplog.text
    else:
        assert len(vorschauen) == 1
    if fall.startswith("bewertungsdaten"):
        assert bewerter.aufrufe == []  # ohne Bewertungsdaten keine Anfrage an die Flaechen-KI
    # Die Karte erscheint wie bisher; kein Fehler fuer die Lernenden.
    assert len(_karten(penecho)) == 1 and r.json()["urteil"] == "Richtig."


def test_learning_weg_mit_ki_bewertung_ohne_wiederholung() -> None:
    # Learning nicht erreichbar ist keine Ablehnung der externen Bewertung: kein zweiter Aufruf,
    # die bekannte Fehlerbehandlung (503, Sperre bleibt) greift.
    bewerter = FakeBewerter(EXTERN)
    offen = turn(TASK_1, typ="numeric", mode="training")
    gut = {"session": offen, "vorbewertung": _vor()}
    lern, client, penecho, _ = _ki_sitzung(
        "numeric", bewerter=bewerter, vorschau=[LearningWeg("learning_task_preview"), gut])
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 503, r.text
    vorschauen = _aufrufe(lern, "learning_task_preview")
    assert len(vorschauen) == 1 and "externe_bewertung" in vorschauen[0]["payload"]
    assert _karten(penecho) == []


def test_ruecklauf_zweiter_fehler_geht_durch_die_bekannte_behandlung() -> None:
    bewerter = FakeBewerter(EXTERN)
    fehler = LearningAbgelehnt("conflict", "session_revision_conflict")
    lern, client, _, _ = _ki_sitzung("numeric", bewerter=bewerter, vorschau=[fehler, fehler])
    r = client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1})
    assert r.status_code == 409
    assert len(_aufrufe(lern, "learning_task_preview")) == 2  # genau ein Wiederholungsversuch


def test_ohne_bewerter_oder_schluessel_keine_bewertungsdaten() -> None:
    for bewerter in (None, FakeBewerter(EXTERN, aktiv=False)):
        lern, client, _, _ = _ki_sitzung("numeric", bewerter=bewerter)
        assert client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1}).status_code == 200
        assert "learning_task_bewertungsdaten" not in lern.tools()
        (kw,) = _aufrufe(lern, "learning_task_preview")
        assert set(kw["payload"]) == {"handschrift_png_base64"}


def test_korrektur_bleibt_ohne_ki_bewertung() -> None:
    bewerter = FakeBewerter(EXTERN)
    lern, client, _, _ = _ki_sitzung("numeric", bewerter=bewerter)
    r = client.post(f"/lernen/api/sitzungen/{SID}/korrektur",
                    json={"revision": 1, "vorbewertung_id": VOR_ID, "answer": {"value": "2"}})
    assert r.status_code == 200, r.text
    assert "learning_task_bewertungsdaten" not in lern.tools() and bewerter.aufrufe == []
    assert "externe_bewertung" not in _aufrufe(lern, "learning_task_preview")[0]["payload"]


def test_bewertungsdaten_nicht_an_den_browser(caplog: pytest.LogCaptureFixture) -> None:
    bewerter = FakeBewerter(EXTERN)
    _, client, _, _ = _ki_sitzung("numeric", bewerter=bewerter)
    with caplog.at_level("DEBUG"):
        antworten = [
            client.post(f"/lernen/api/sitzungen/{SID}/abgeben", json={"revision": 1}),
            client.get("/lernen/api/kurse"),
        ]
    for r in antworten:
        for wort in ("musterloesung", "kriterien", GEHEIM, "GEHEIMES-KRITERIUM"):
            assert wort not in r.text
    assert GEHEIM not in caplog.text and "GEHEIMES-KRITERIUM" not in caplog.text


def test_listenabgabe_mit_ki_bewertung_gruppe() -> None:
    gk = gruppen_lern_kennung(PA, GID)
    laeufe = Laeufe({gk: SID_GA})
    ereignisse: list[str] = []
    bewerter = FakeBewerter(EXTERN, ereignisse=ereignisse)
    laeufe.lern.antworten["learning_task_bewertungsdaten"] = lambda kw: (
        ereignisse.append("bewertungsdaten") or {**BEWERTUNGSDATEN, "attempt_id": kw["payload"]["attempt_id"]})
    client, _, _, _ = _gruppen_bruecke(laeufe, bewerter=bewerter)
    bruecke = client.app
    try:
        assert client.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        assert client.post(f"/lernen/api/sitzungen/{SID_GA}/abgeben", json={"nummer": 1}).status_code == 202
        _bis(lambda: bruecke._modellspur.wartend("") == 0 and bruecke._flaechenspur.wartend("") == 0)
        assert ereignisse == ["bewertungsdaten", "bewerter"]
        aid = laeufe.eintraege[gk][0]["attempt_id"]
        (daten_kw,) = _aufrufe(laeufe.lern, "learning_task_bewertungsdaten")
        assert daten_kw["payload"] == {"attempt_id": aid} and daten_kw["session_id"] == SID_GA
        (kw,) = _aufrufe(laeufe.lern, "learning_task_preview")
        assert kw["payload"]["attempt_id"] == aid and kw["payload"]["externe_bewertung"] == EXTERN
        # Der Akteur ist die Lern-Kennung der Person in der Gruppe, nie die Person selbst.
        bewertungsakteure = [a for (t, _), a in zip(laeufe.lern.aufrufe, laeufe.lern.akteure)
                             if t in ("learning_task_bewertungsdaten", "learning_task_preview")]
        assert bewertungsakteure == [gk, gk] and gk != PA
    finally:
        bruecke.beenden()


# ---- Task 5: KI-Schritte je Aufgabe -----------------------------------------------------------------

def _ki(client: TestClient, sid: str, **body: Any) -> Any:
    return client.post(f"/lernen/api/sitzungen/{sid}/ki-schritt", json=body)


def _ki_zeilen(ordner) -> list[str]:
    (md,) = _md_dateien(ordner)
    return [z for z in md.read_text(encoding="utf-8").splitlines() if z.startswith("KI genutzt:")]


def test_ki_schritt_wird_je_aufgabe_gezaehlt(tmp_path) -> None:
    import dataclasses

    ordner = tmp_path / "wissen"
    ll = ListenLern(2)
    client, _ = _zf_offen(ll, einstellung=dataclasses.replace(E, zusammenfassung_ordner=str(ordner)))
    try:
        for aktion in ("solve", "hint", "hint"):
            r = _ki(client, SID, nummer=1, aktion=aktion, ausloeser="manuell")
            assert r.status_code == 204 and r.content == b"", r.text
        assert client.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert _ki_zeilen(ordner) == ["KI genutzt: Lösen, Hinweis ×2", "KI genutzt: –"]
    finally:
        client.app.beenden()


def test_ki_schritt_fremde_sitzung_404() -> None:
    ll = ListenLern(2)
    client, _, _ = _listen_offen(ll)
    try:
        fremd = als(TestClient(client.app), "person-fremd")
        assert _ki(fremd, SID, nummer=1, aktion="solve", ausloeser="auto").status_code == 404
        assert client.app._ki_schritte_lesen(SID, 1) == ()
    finally:
        client.app.beenden()


def test_ki_schritt_unbekannte_aktion_wird_frei() -> None:
    ll = ListenLern(2)
    client, _, _ = _listen_offen(ll)
    try:
        for aktion in ("hack", 5, None, "x" * 5000):
            assert _ki(client, SID, nummer=1, aktion=aktion, ausloeser="auto").status_code == 204
        assert client.app._ki_schritte_lesen(SID, 1) == ("frei",) * 4
    finally:
        client.app.beenden()


def test_ki_schritt_ungueltige_koerper() -> None:
    ll = ListenLern(2)
    client, _, _ = _listen_offen(ll)
    try:
        for body in ({"nummer": 0, "aktion": "solve", "ausloeser": "auto"},
                     {"nummer": True, "aktion": "solve", "ausloeser": "auto"},
                     {"nummer": "1", "aktion": "solve", "ausloeser": "auto"},
                     {"aktion": "solve", "ausloeser": "auto"},
                     {"nummer": 1, "aktion": "solve", "ausloeser": "sonst"},
                     {"nummer": 1, "aktion": "solve"}):
            assert _ki(client, SID, **body).status_code == 400, body
        assert client.app._ki_schritte_lesen(SID, 1) == ()
    finally:
        client.app.beenden()


def test_ki_schritte_sind_begrenzt() -> None:
    ll = ListenLern(2)
    client, _, _ = _listen_offen(ll)
    try:
        for _ in range(205):
            _ki(client, SID, nummer=1, aktion="hint", ausloeser="auto")
        assert len(client.app._ki_schritte_lesen(SID, 1)) == 200
    finally:
        client.app.beenden()


def test_ki_schritte_ueberstehen_neustart(tmp_path) -> None:
    import dataclasses

    datei = tmp_path / "kursende.json"
    ordner = tmp_path / "wissen"
    ein = dataclasses.replace(E, zusammenfassung_ordner=str(ordner))
    ll = ListenLern(2)
    client, _, _ = _listen_offen(ll, kursende=datei, einstellung=ein)
    try:
        assert _ki(client, SID, nummer=2, aktion="explain", ausloeser="manuell").status_code == 204
    finally:
        client.app.beenden()
    client2, _, _ = _listen_offen(ll, kursende=datei, einstellung=ein)
    try:
        assert _ki(client2, SID, nummer=2, aktion="explain", ausloeser="auto").status_code == 204
        assert client2.get(f"/lernen/api/sitzungen/{SID}/zusammenfassung.pdf").status_code == 200
        assert _ki_zeilen(ordner) == ["KI genutzt: –", "KI genutzt: Erklären ×2"]
    finally:
        client2.app.beenden()


def test_ki_schritt_gruppe_eigene_person() -> None:
    ga, gb = gruppen_lern_kennung(PA, GID), gruppen_lern_kennung(PB, GID)
    laeufe = Laeufe({ga: SID_GA, gb: SID_GB})
    a, _, _, _ = _gruppen_bruecke(laeufe)
    b = als(TestClient(a.app), PB)
    b.headers[KOPF] = KB
    bruecke = a.app
    try:
        assert a.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        assert b.post(f"/lernen/api/gruppen/{GID}/oeffnen").status_code == 200
        assert _ki(a, SID_GA, nummer=1, aktion="hint", ausloeser="manuell").status_code == 204
        # B schreibt nie in As Sitzung ...
        assert _ki(b, SID_GA, nummer=1, aktion="solve", ausloeser="manuell").status_code == 404
        # ... sondern nur in die eigene.
        assert _ki(b, SID_GB, nummer=1, aktion="plot", ausloeser="auto").status_code == 204
        assert bruecke._ki_schritte_lesen(SID_GA, 1) == ("hint",)
        assert bruecke._ki_schritte_lesen(SID_GB, 1) == ("plot",)
    finally:
        bruecke.beenden()
