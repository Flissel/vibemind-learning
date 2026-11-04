"""Bruecke der Lern-Huelle: JSON-API unter `/lernen/api/` zwischen Seite, Learning und PenEcho.

Starlette mit async Handlern; die Clients (LernMcp, PenEchoMcp, httpx.Client) sind
synchron und laufen im Threadpool. Routen, die Sitzung, Flaeche oder Sperre veraendern
(oeffnen, antwort, weiter, zeichnung), laufen nacheinander unter einer Sperre des
Ablaufs; PenEchoMcp ist nicht fuer parallele Aufrufe gebaut.

Die Lernenden-ID kommt nur aus der Einstellung. Geloggt werden nur Typnamen.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, NoReturn, TypeVar
from uuid import UUID, uuid5

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .einstellung import Einstellung, Kurs
from .lern_mcp import LearningAbgelehnt, LearningWeg, LernMcp
from .penecho_mcp import BEREICH_BREITE, BEREICH_HOEHE, PenEchoMcp, PenEchoNichtErreichbar, PenEchoSitzungUnbekannt
from .sperre import Sperre

log = logging.getLogger(__name__)

T = TypeVar("T")

# Feste Texte (Plan, global.md).
NICHT_ERREICHBAR = "Learning ist gerade nicht erreichbar. Ihre Fläche bleibt erhalten."
SCHON_ABGEGEBEN = "Diese Aufgabe wurde schon abgegeben. Ich lade den aktuellen Stand."
NICHT_ANGENOMMEN = "Diese Abgabe wurde nicht angenommen. Bitte prüfen Sie Ihre Eingabe."  # Ruling H
KEIN_KURS = "Dieser Kurs hat noch keine freigegebenen Aufgaben."
WIRD_GEPRUEFT = "Ihre Abgabe wird noch geprüft. Sie zählt, sobald die Prüfung fertig ist."
FLAECHE_WEG = "Die Fläche ist gerade nicht erreichbar."
# Nach dem Fork (apps/web/lib/learning/practiceModel.ts). Die Huelle zeigt keine Quellenbelege;
# nach der Abgabe ist PenEchos KI frei und erklaert den Weg auf der Flaeche.
_RUECKMELDUNG = {
    "correct": "Richtig – gut gemacht.",
    "partial": "Teilweise richtig. Lassen Sie sich jetzt auf der Fläche von der KI erklären, was noch fehlt.",
    "incorrect": "Noch nicht richtig. Lassen Sie sich den Lösungsweg jetzt auf der Fläche von der KI erklären.",
    "needs_review": WIRD_GEPRUEFT,
}
KEIN_HINWEIS = "Kein Hinweis verfügbar."
KEIN_HINWEIS_PRUEFUNG = "Im Prüfungsmodus gibt es keine Hinweise."
SMILEY_UNBEKANNT = "Bitte wählen Sie eines der drei Gesichter."
ZU_VIELE_STICHWORTE = "Bitte nennen Sie höchstens drei Stichworte."
# Von der Huelle: Bedienfehler, die die eigene Seite nicht ausloest.
UNBEKANNTER_KURS = "Diesen Kurs gibt es hier nicht."
UNGUELTIG = "Ungültige Eingabe."
KEINE_ZEICHENAUFGABE = "Die aktuelle Aufgabe ist keine Zeichenaufgabe."
ERKLAER_FRAGE = "Erklär mir das."

TRAINING_BLUEPRINT = {"max_attempts": 20}
MAX_BODY = 256 * 1024
MAX_FRAGE = 500
SMILEYS = frozenset({"gut", "mittel", "schlecht"})
_PNG = b"\x89PNG\r\n\x1a\n"
_NAMENSRAUM = UUID("0b8f6c1e-3a7d-4f52-9c1e-6d2a4b7e9f30")
# Learning-Codes (mcp/tools/sessions.py, mcp/tools/canvas.py), bei denen die Abgabe schon
# verbucht oder ueberholt ist (Ruling H).
_SCHON_ABGEGEBEN_CODES = frozenset({
    "session_revision_conflict", "session_state_conflict", "canvas_revision_conflict",
})
_SERVICE_HEADER = "X-LearnHouse-Learning-Service-Key"
_ACTOR_HEADER = "X-Learning-Actor"
# Bilder der Aufgaben (gezeichnete Graphen, Terminalfenster) liegen bei learning-api; die Seite holt sie
# ueber die eigene Route. Gleiches Namensmuster wie der Fork (practice.py `_MEDIA_PATH`).
MEDIEN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(png|jpg|webp)$")
_MEDIEN_PFAD = re.compile(r"^/lernsequenz-medien/([A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:png|jpg|webp))$")
MEDIEN_ROUTE = "/lernen/api/medien/"
_MEDIEN_ART = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
MAX_MEDIUM = 8 * 1024 * 1024


def _typ(fehler: BaseException) -> str:
    return type(fehler).__name__


class _Antwort(Exception):
    """Vorzeitige JSON-Antwort aus der Ablauflogik."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


@dataclass
class _Sitzung:
    kurs: Kurs
    sid: str                        # Learning-Sitzung; zugleich Schluessel der Flaeche (Ruling L)
    aufgabe_id: str | None = None   # aktuelle Aufgabe laut letztem Turn
    nummer: int | None = None       # Flaechenbereich der aktuellen Aufgabe (Ruling B)
    auf_flaeche: bool = False       # aktuelle Aufgabe steht schon auf der Flaeche


def _uuid(wert: str) -> str | None:
    try:
        return str(UUID(str(wert)))
    except ValueError:
        return None


def _task(turn: dict[str, Any]) -> dict[str, Any] | None:
    task = turn.get("task")
    if turn.get("state") != "active" or not isinstance(task, dict) or not isinstance(task.get("id"), str):
        return None
    return task


def _nummer(turn: dict[str, Any]) -> int:
    progress = turn.get("progress")
    completed = progress.get("completed") if isinstance(progress, dict) else None
    return (completed if isinstance(completed, int) and not isinstance(completed, bool) else 0) + 1


def _medien(turn: dict[str, Any]) -> dict[str, Any]:
    """Bildfelder der Aufgabe auf die eigene Route umschreiben, alles andere darin entfernen (wie der Fork)."""
    task = turn.get("task")
    presentation = task.get("presentation") if isinstance(task, dict) else None
    if not isinstance(presentation, dict):
        return turn

    def fix(eintrag: dict[str, Any]) -> None:
        if "image" not in eintrag or eintrag["image"] is None:
            return
        bild = eintrag["image"]
        treffer = _MEDIEN_PFAD.fullmatch(bild) if isinstance(bild, str) else None
        if treffer:
            eintrag["image"] = MEDIEN_ROUTE + treffer.group(1)
        else:
            eintrag.pop("image")

    fix(presentation)
    for seite in ("left", "right"):
        eintraege = presentation.get(seite)
        for eintrag in eintraege if isinstance(eintraege, list) else []:
            if isinstance(eintrag, dict):
                fix(eintrag)
    return turn


def _turn(result: dict[str, Any]) -> dict[str, Any]:
    turn = result.get("session")
    if not isinstance(turn, dict):
        raise LearningWeg("session")
    return _medien(turn)


def rueckmeldung_text(turn: dict[str, Any]) -> str | None:
    """Text der Notizkarte, nur aus Learnings Rueckmeldung (Zustand und Kriterien-Texte)."""
    feedback = turn.get("feedback")
    if not isinstance(feedback, dict):
        return None
    zeilen = []
    kopf = _RUECKMELDUNG.get(str(feedback.get("state")))
    if kopf:
        zeilen.append(kopf)
    for k in feedback.get("criteria") or []:
        if not isinstance(k, dict):
            continue
        score, text = k.get("score"), k.get("feedback")
        if isinstance(score, (int, float)) and score < 1 and isinstance(text, str) and text.strip():
            beschreibung = k.get("description")
            beschreibung = beschreibung.rstrip(" .:") if isinstance(beschreibung, str) else ""
            text = _ohne_quellenmarken(text)
            zeilen.append(f"{beschreibung}: {text}" if beschreibung else text)
    return "\n\n".join(zeilen) or None


# mcp/tools/tutor.py nummeriert die Quellen als S1, S2, ...; die Huelle zeigt sie nicht an.
_QUELLENMARKE = re.compile(r"\s*\((?:vgl\.\s*)?S\d+(?:\s*,\s*S\d+)*\)")


def _ohne_quellenmarken(text: str) -> str:
    return _QUELLENMARKE.sub("", text)


def _als_png(bild: bytes) -> bytes:
    """Learning nimmt nur PNG; PenEcho liefert unter Groessendruck auch JPEG/WebP."""
    if bild.startswith(_PNG):
        return bild
    from PIL import Image

    with Image.open(io.BytesIO(bild)) as roh:
        ziel = io.BytesIO()
        roh.save(ziel, format="PNG")
    return ziel.getvalue()


class Bruecke:
    def __init__(
        self,
        einstellung: Einstellung,
        lern: LernMcp,
        penecho: PenEchoMcp,
        sperre: Sperre,
        http: httpx.Client,
    ) -> None:
        self._e = einstellung
        self._lern = lern
        self._penecho = penecho
        self._sperre = sperre
        self._http = http
        self._kurse = {k.course_id: k for k in einstellung.kurse}
        self._sitzungen: dict[str, _Sitzung] = {}
        self._ablauf = asyncio.Lock()
        p = "/lernen/api"
        self._app = Starlette(routes=[
            Route(f"{p}/kurse", self._r_kurse, methods=["GET"]),
            Route(f"{p}/kurse/{{course_id}}/oeffnen", self._r_oeffnen, methods=["POST"]),
            Route(f"{p}/kurse/{{course_id}}/bewertung", self._r_bewertung, methods=["GET", "POST"]),
            Route(f"{p}/sitzungen/{{sid}}/antwort", self._r_antwort, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/weiter", self._r_weiter, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/zeichnung", self._r_zeichnung, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/erklaeren", self._r_erklaeren, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/hinweis", self._r_hinweis, methods=["POST"]),
            Route(f"{p}/notizen", self._r_notizen, methods=["GET"]),
            Route(f"{p}/medien/{{name}}", self._r_medien, methods=["GET"]),
        ])

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self._app(scope, receive, send)

    # ---- Rahmen -----------------------------------------------------------
    @staticmethod
    def _json(status: int, body: Any) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    async def _ausfuehren(self, arbeit: Callable[[], Any], *, ablauf: bool) -> JSONResponse:
        try:
            if ablauf:
                async with self._ablauf:
                    body = await run_in_threadpool(arbeit)
            else:
                body = await run_in_threadpool(arbeit)
        except _Antwort as antwort:
            return self._json(antwort.status, antwort.body)
        except LearningWeg:
            # Die Sperre bleibt, wie sie ist.
            return self._json(503, {"meldung": NICHT_ERREICHBAR})
        except LearningAbgelehnt as fehler:
            return self._json(409, {"meldung": fehler.nachricht, "code": fehler.code})
        return self._json(200, body)

    @staticmethod
    async def _body(request: Request) -> dict[str, Any]:
        roh = await request.body()
        if len(roh) > MAX_BODY:
            raise _Antwort(413, {"meldung": UNGUELTIG})
        if not roh.strip():
            return {}
        try:
            wert = json.loads(roh)
        except ValueError:
            raise _Antwort(400, {"meldung": UNGUELTIG}) from None
        if not isinstance(wert, dict):
            raise _Antwort(400, {"meldung": UNGUELTIG})
        return wert

    async def _mit_body(
        self, request: Request, arbeit: Callable[[dict[str, Any]], Any], *, ablauf: bool
    ) -> JSONResponse:
        try:
            body = await self._body(request)
        except _Antwort as antwort:
            return self._json(antwort.status, antwort.body)
        return await self._ausfuehren(lambda: arbeit(body), ablauf=ablauf)

    def _kurs(self, course_id: str) -> Kurs:
        kurs = self._kurse.get(_uuid(course_id) or "")
        if kurs is None:
            raise _Antwort(404, {"meldung": UNBEKANNTER_KURS})
        return kurs

    def _sitzung(self, sid: str) -> tuple[str, _Sitzung, dict[str, Any] | None]:
        """Sitzung aus dem Speicher; sonst einmal `learning_task_next` lesen und den Kurs pruefen."""
        norm = _uuid(sid)
        if norm is None:
            raise _Antwort(404, {"meldung": UNBEKANNTER_KURS})
        bekannt = self._sitzungen.get(norm)
        if bekannt is not None:
            return norm, bekannt, None
        try:
            turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=norm))
        except LearningAbgelehnt:
            # Fremde oder unbekannte Sitzung: Learning prueft den Besitz, die Huelle sagt nur 404.
            raise _Antwort(404, {"meldung": UNBEKANNTER_KURS}) from None
        kurs = self._kurse.get(_uuid(str(turn.get("course_id"))) or "")
        if kurs is None:
            raise _Antwort(404, {"meldung": UNBEKANNTER_KURS})
        task = _task(turn)
        sitzung = _Sitzung(kurs, norm, task["id"] if task else None, _nummer(turn) if task else None)
        self._sitzungen[norm] = sitzung
        return norm, sitzung, turn

    # ---- PenEcho ----------------------------------------------------------
    def _auf_flaeche(self, sitzung: _Sitzung, aktion: Callable[[str], T]) -> T:
        """Canvas-Aufruf auf der Flaeche dieser Lernsitzung (Ruling L).

        Kennt PenEcho die Sitzung nicht mehr, einmal neu anlegen und wiederholen.
        """
        titel = f"{sitzung.kurs.titel} · {date.today():%d.%m.%Y}"
        session_id = self._penecho.sitzung(sitzung.sid, titel)
        try:
            return aktion(session_id)
        except PenEchoSitzungUnbekannt:
            self._penecho.vergessen(sitzung.sid)
            return aktion(self._penecho.sitzung(sitzung.sid, titel))

    def _flaeche_still(self, sitzung: _Sitzung, aktion: Callable[[str], Any]) -> bool:
        try:
            self._auf_flaeche(sitzung, aktion)
        except (PenEchoNichtErreichbar, OSError, ValueError) as fehler:
            # ValueError: Aufgabennummer ausserhalb der 20 Bereiche der Flaeche.
            log.warning("Flaeche uebersprungen: %s", _typ(fehler))
            return False
        return True

    def _zeigen(self, sitzung: _Sitzung, turn: dict[str, Any]) -> tuple[int | None, bool]:
        """Aufgabe auf die Flaeche setzen und die KI sperren; ohne Aufgabe die Sperre loesen."""
        task = _task(turn)
        if task is None:
            sitzung.aufgabe_id, sitzung.nummer, sitzung.auf_flaeche = None, None, False
            self._frei(sitzung)
            return None, False
        nummer = _nummer(turn)
        aufgabe_id = task["id"]
        self._sperre.offen(aufgabe_id)
        if sitzung.aufgabe_id == aufgabe_id and sitzung.nummer == nummer and sitzung.auf_flaeche:
            return nummer, True
        text = str(task.get("prompt") or "")
        ok = self._flaeche_still(
            sitzung, lambda s: self._penecho.aufgabe_setzen(s, aufgabe_id, text, nummer)
        )
        sitzung.aufgabe_id, sitzung.nummer, sitzung.auf_flaeche = aufgabe_id, nummer, ok
        return nummer, ok

    def _frei(self, sitzung: _Sitzung) -> None:
        """Die Sperre ist global: nur loesen, wenn keine andere Sitzung eine offene Aufgabe hat.

        Andere bekannte Sitzungen liest die Bruecke bei Learning neu, statt der gemerkten Aufgabe zu
        trauen (sie kann anderswo, etwa im Fork, laengst abgegeben sein). Ist Learning weg, bleibt es
        gesperrt; lehnt Learning eine gemerkte Sitzung ab, wird sie vergessen und ihr Kurs per Ueberblick
        geprueft. Kurse ohne bekannte Sitzung (etwa nach einem Neustart) fragt die Bruecke per Ueberblick;
        scheitert das, bleibt es gesperrt.
        """
        for andere_sid, andere in list(self._sitzungen.items()):
            if andere is sitzung:
                continue
            try:
                turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=andere_sid))
            except LearningWeg as fehler:
                log.warning("Sperre bleibt, Sitzung nicht lesbar: %s", _typ(fehler))
                self._sperre.unbekannt()
                return
            except LearningAbgelehnt as fehler:
                log.warning("Gemerkte Sitzung abgelehnt, vergessen: %s", _typ(fehler))
                self._sitzungen.pop(andere_sid, None)
                continue
            task = _task(turn)
            nummer = _nummer(turn) if task else None
            neu = task["id"] if task else None
            if neu != andere.aufgabe_id or nummer != andere.nummer:
                andere.auf_flaeche = False
            andere.aufgabe_id, andere.nummer = neu, nummer
            if task is not None:
                self._sperre.offen(task["id"])
                return
        bekannt = {s.kurs.course_id for s in list(self._sitzungen.values())} | {sitzung.kurs.course_id}
        for kurs in self._e.kurse:
            if kurs.course_id in bekannt:
                continue
            try:
                ueberblick = self._lern.rufe(
                    "learning_course_overview", "learning.course.overview", course_id=kurs.course_id
                )
            except (LearningWeg, LearningAbgelehnt) as fehler:
                log.warning("Sperre bleibt, Ueberblick fehlgeschlagen: %s", _typ(fehler))
                self._sperre.unbekannt()
                return
            laufend = ueberblick.get("session")
            task = _task(laufend) if isinstance(laufend, dict) else None
            andere_sid = _uuid(str(laufend.get("session_id"))) if isinstance(laufend, dict) else None
            if task is not None:
                if andere_sid is not None:
                    self._sitzungen.setdefault(andere_sid, _Sitzung(kurs, andere_sid, task["id"], _nummer(laufend)))
                self._sperre.offen(task["id"])
                return
        self._sperre.frei()

    def _vormerken(self, sitzung: _Sitzung, turn: dict[str, Any]) -> int | None:
        """Naechste Aufgabe merken, ohne sie zu zeigen (das macht `weiter`)."""
        task = _task(turn)
        nummer = _nummer(turn) if task else None
        neu = task["id"] if task else None
        if neu != sitzung.aufgabe_id or nummer != sitzung.nummer:
            sitzung.auf_flaeche = False
        sitzung.aufgabe_id, sitzung.nummer = neu, nummer
        return nummer

    def _rueckmeldung(self, sitzung: _Sitzung, turn: dict[str, Any], aufgabe_id: str, nummer: int) -> tuple[str | None, bool]:
        text = rueckmeldung_text(turn)
        if not text:
            return None, False
        titel = f"Rückmeldung zu Aufgabe {nummer}"
        ok = self._flaeche_still(
            sitzung, lambda s: self._penecho.notizkarte(s, aufgabe_id, titel, text)
        )
        return text, ok

    def _begriffe(self, kurs: Any) -> list[Any] | None:
        """Aktuelle Beherrschung nach einer Abgabe; None, wenn Learning sie gerade nicht liefert."""
        try:
            ueberblick = self._lern.rufe("learning_course_overview", "learning.course.overview", course_id=kurs.course_id)
        except (LearningWeg, LearningAbgelehnt) as fehler:
            log.warning("Begriffe nach Abgabe nicht geladen: %s", _typ(fehler))
            return None
        begriffe = ueberblick.get("begriffe")
        return begriffe if isinstance(begriffe, list) else None

    def _stand_neu(self, sid: str, sitzung: _Sitzung, code: str) -> NoReturn:
        """Nach einer Ablehnung den aktuellen Stand laden; 409 mit Turn (Ruling H fuer den Text)."""
        turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=sid))
        self._vormerken(sitzung, turn)
        if _task(turn) is None:
            self._frei(sitzung)
        meldung = SCHON_ABGEGEBEN if code in _SCHON_ABGEGEBEN_CODES else NICHT_ANGENOMMEN
        raise _Antwort(409, {"code": code, "meldung": meldung, "turn": turn})

    # ---- Routen -----------------------------------------------------------
    async def _r_kurse(self, request: Request) -> JSONResponse:
        return self._json(200, [{"course_id": k.course_id, "titel": k.titel} for k in self._e.kurse])

    async def _r_oeffnen(self, request: Request) -> JSONResponse:
        course_id = request.path_params["course_id"]
        return await self._ausfuehren(lambda: self._oeffnen(course_id), ablauf=True)

    def _oeffnen(self, course_id: str) -> dict[str, Any]:
        kurs = self._kurs(course_id)
        ueberblick = self._lern.rufe("learning_course_overview", "learning.course.overview", course_id=kurs.course_id)
        revision = ueberblick.get("course_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise _Antwort(409, {"meldung": KEIN_KURS})
        laufend = ueberblick.get("session")
        sid = _uuid(str(laufend.get("session_id"))) if isinstance(laufend, dict) else None
        if sid is None:
            gestartet = _turn(self._lern.rufe(
                "learning_session_start", "learning.session.start", course_id=kurs.course_id, schreibend=True,
                payload={"course_revision": revision, "mode": "training", "blueprint": dict(TRAINING_BLUEPRINT)},
            ))
            sid = _uuid(str(gestartet.get("session_id")))
            if sid is None:
                raise LearningWeg("session_id")
        turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=sid))
        # Learning nennt fuer diesen Kurs genau diese Sitzung: aeltere gemerkte Sitzungen des Kurses
        # (abgebrochen, ersetzt) wuerden sonst die Sperre mit ihrer alten Aufgabe zuhalten.
        for alt in [k for k, s in self._sitzungen.items() if s.kurs.course_id == kurs.course_id and k != sid]:
            del self._sitzungen[alt]
        sitzung = self._sitzungen.get(sid)
        if sitzung is None or sitzung.kurs.course_id != kurs.course_id:
            sitzung = self._sitzungen[sid] = _Sitzung(kurs, sid)
        nummer, flaeche = self._zeigen(sitzung, turn)
        begriffe = ueberblick.get("begriffe")
        return {
            "kurs": {"course_id": kurs.course_id, "titel": kurs.titel},
            "begriffe": begriffe if isinstance(begriffe, list) else [],
            "turn": turn,
            "nummer": nummer,
            "flaeche": flaeche,
        }

    async def _r_antwort(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._mit_body(request, lambda body: self._antwort(sid, body), ablauf=True)

    def _antwort(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        revision, answer = body.get("revision"), body.get("answer")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1 or not isinstance(answer, dict):
            raise _Antwort(400, {"meldung": UNGUELTIG})
        sid, sitzung, _ = self._sitzung(sid_roh)
        beantwortet_id = sitzung.aufgabe_id or f"r{revision}"
        beantwortet_nr = sitzung.nummer
        try:
            result = self._lern.rufe(
                "learning_task_answer", "learning.task.answer", session_id=sid, expected_revision=revision,
                payload={"answer": answer}, schreibend=True,
            )
        except LearningAbgelehnt as fehler:
            self._stand_neu(sid, sitzung, fehler.code)
        turn = _turn(result)
        # Abgegeben: KI frei (sofern kein anderer Kurs eine offene Aufgabe hat).
        # Erst `weiter` zeigt die naechste Aufgabe und sperrt wieder.
        self._frei(sitzung)
        if beantwortet_nr is None:
            progress = turn.get("progress")
            beantwortet_nr = max(int(progress.get("completed") or 1), 1) if isinstance(progress, dict) else 1
        text, flaeche = self._rueckmeldung(sitzung, turn, beantwortet_id, beantwortet_nr)
        sitzung.auf_flaeche = False
        nummer = self._vormerken(sitzung, turn)
        return {"turn": turn, "rueckmeldung": text, "nummer": nummer, "flaeche": flaeche,
                "begriffe": self._begriffe(sitzung.kurs)}

    async def _r_weiter(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._ausfuehren(lambda: self._weiter(sid), ablauf=True)

    def _weiter(self, sid_roh: str) -> dict[str, Any]:
        sid, sitzung, turn = self._sitzung(sid_roh)
        if turn is None:
            turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=sid))
        nummer, flaeche = self._zeigen(sitzung, turn)
        return {"turn": turn, "nummer": nummer, "flaeche": flaeche}

    async def _r_zeichnung(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._mit_body(request, lambda body: self._zeichnung(sid, body), ablauf=True)

    def _zeichnung(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        revision = body.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise _Antwort(400, {"meldung": UNGUELTIG})
        sid, sitzung, _ = self._sitzung(sid_roh)
        nummer = sitzung.nummer
        if nummer is None:
            self._stand_neu(sid, sitzung, "session_state_conflict")
        # 1. Schnappschuss des Aufgabenbereichs; ohne Flaeche keine Abgabe.
        try:
            bild = self._auf_flaeche(sitzung, lambda s: self._penecho.schnappschuss(s, nummer))
            png = _als_png(bild)
        except Exception as fehler:  # noqa: BLE001 - PenEcho weg oder Bild unlesbar
            log.warning("Schnappschuss fehlgeschlagen: %s", _typ(fehler))
            raise _Antwort(503, {"meldung": FLAECHE_WEG}) from None
        # 2. Aktueller Stand: Revision der Seite muss stimmen (sonst Doppelabgabe).
        stand = _turn(self._lern.rufe("learning_progress_show", "learning.progress.show", session_id=sid))
        task = _task(stand)
        if stand.get("session_revision") != revision or task is None:
            self._stand_neu(sid, sitzung, "session_revision_conflict" if task else "session_state_conflict")
        if task.get("type") != "penecho_canvas":
            raise _Antwort(400, {"meldung": KEINE_ZEICHENAUFGABE})
        activity_id = task["id"]
        # Dieselbe Referenz fuer dieselbe Sitzung: die Learning-Flaeche bindet sie beim ersten Oeffnen.
        ref = "hu-z-" + uuid5(_NAMENSRAUM, f"{sid}:{self._e.actor_id}").hex
        try:
            # 3. open -> save -> submit wie deployment/zeichnen_routes.py.
            offen = self._lern.rufe(
                "learning_canvas_open", "learning.canvas.open", session_id=sid,
                payload={"activity_id": activity_id, "session_ref": ref},
            )
            canvas = offen.get("canvas")
            if not isinstance(canvas, dict) or not isinstance(canvas.get("document"), dict):
                raise LearningWeg("canvas")
            canvas_rev = int(canvas["revision"])
            dokument = {
                **canvas["document"],
                "revision": canvas_rev,
                "objects": [{
                    "object_id": "schnappschuss",
                    "object_type": "image",
                    "x": 0, "y": 0, "width": BEREICH_BREITE, "height": BEREICH_HOEHE, "rotation": 0,
                    "asset_ref": f"artifact://learning/canvas/{sid}/{canvas_rev + 1}/snapshot.png",
                }],
            }
            self._lern.rufe(
                "learning_canvas_save", "learning.canvas.save", session_id=sid, expected_revision=canvas_rev,
                schreibend=True,
                payload={"session_ref": ref, "document": dokument,
                         "snapshot_png_base64": base64.b64encode(png).decode("ascii")},
            )
            abgabe = f"{sid}:{revision}"
            eingereicht = self._lern.rufe(
                "learning_canvas_submit", "learning.canvas.submit", session_id=sid, expected_revision=canvas_rev + 1,
                schreibend=True,
                # Die Person hat auf der Seite „Abgeben“ gedrueckt; das ist die Bestaetigung.
                confirmation={"confirmed": True, "approval_ref": "hu-zeichnung-" + uuid5(_NAMENSRAUM, abgabe).hex[:16]},
                payload={
                    "session_ref": ref,
                    "submission_id": str(uuid5(_NAMENSRAUM, "abgabe:" + abgabe)),
                    "adaptive_session_revision": revision,
                },
            )
        except LearningAbgelehnt as fehler:
            self._stand_neu(sid, sitzung, fehler.code)
        except (KeyError, TypeError, ValueError):
            raise LearningWeg("canvas") from None
        turn = _turn(eingereicht)
        self._frei(sitzung)
        bewertung = eingereicht.get("evaluation") if isinstance(eingereicht.get("evaluation"), dict) else {}
        text, flaeche = self._rueckmeldung(sitzung, turn, activity_id, nummer)
        sitzung.auf_flaeche = False
        naechste = self._vormerken(sitzung, turn)
        return {
            "turn": turn,
            "bewertung": {"punkte": bewertung.get("score"), "angenommen": bewertung.get("accepted")},
            "meldung": None if bewertung.get("accepted") else WIRD_GEPRUEFT,
            "rueckmeldung": text,
            "nummer": naechste,
            "flaeche": flaeche,
            "begriffe": self._begriffe(sitzung.kurs),
        }

    async def _r_erklaeren(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._mit_body(request, lambda body: self._erklaeren(sid, body), ablauf=False)

    def _erklaeren(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        frage = body.get("frage")
        if frage is None or (isinstance(frage, str) and not frage.strip()):
            frage = ERKLAER_FRAGE
        if not isinstance(frage, str) or len(frage) > MAX_FRAGE:
            raise _Antwort(400, {"meldung": UNGUELTIG})
        sid, sitzung, _ = self._sitzung(sid_roh)
        # Stufe 1 ohne Schnappschuss: learning_tutor_ask nimmt kein Bild an.
        result = self._lern.rufe(
            "learning_tutor_ask", "learning.tutor.ask", course_id=sitzung.kurs.course_id, session_id=sid,
            payload={"question": frage.strip()},
        )
        tutor = result.get("tutor")
        if not isinstance(tutor, dict) or not isinstance(tutor.get("answer"), str):
            raise LearningWeg("tutor")
        return {"answer": _ohne_quellenmarken(tutor["answer"]),
                "next_step": _ohne_quellenmarken(str(tutor.get("next_step") or ""))}

    async def _r_hinweis(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._ausfuehren(lambda: self._hinweis(sid), ablauf=False)

    def _hinweis(self, sid_roh: str) -> dict[str, Any]:
        sid, _, turn = self._sitzung(sid_roh)
        if turn is None:
            turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=sid))
        task = _task(turn)
        if task is None:
            raise _Antwort(409, {"meldung": KEIN_HINWEIS})
        try:
            result = self._lern.rufe(
                "learning_hint_request", "learning.hint.request", session_id=sid, payload={"task_id": task["id"]},
            )
        except LearningAbgelehnt as fehler:
            meldung = KEIN_HINWEIS_PRUEFUNG if "exam" in fehler.nachricht else KEIN_HINWEIS
            raise _Antwort(409, {"meldung": meldung, "code": fehler.code}) from None
        text = result.get("hint")
        if not isinstance(text, str) or not text:
            raise _Antwort(409, {"meldung": KEIN_HINWEIS})
        return {"hinweis": text}

    # ---- learning-api (REST) ---------------------------------------------
    def _api(self, methode: str, pfad: str, *, json_body: dict[str, Any] | None = None,
             params: dict[str, str] | None = None) -> httpx.Response:
        kopf = {_SERVICE_HEADER: self._e.dienst_schluessel, _ACTOR_HEADER: self._e.actor_id}
        try:
            return self._http.request(
                methode, self._e.api_url.rstrip("/") + pfad, headers=kopf, json=json_body, params=params,
            )
        except httpx.HTTPError as fehler:
            log.warning("learning-api nicht erreichbar: %s", _typ(fehler))
            raise LearningWeg("api") from None

    @staticmethod
    def _api_json(antwort: httpx.Response) -> dict[str, Any]:
        if antwort.status_code != 200:
            log.warning("learning-api: HTTP %s", antwort.status_code)
            raise LearningWeg("api")
        try:
            wert = antwort.json()
        except ValueError:
            raise LearningWeg("api") from None
        if not isinstance(wert, dict):
            raise LearningWeg("api")
        return wert

    async def _r_medien(self, request: Request) -> Response:
        """Ein Aufgabenbild von learning-api (`/lernsequenz-medien/{name}`), nur bekannte Namen und Arten."""
        name = request.path_params["name"]
        treffer = MEDIEN_NAME.fullmatch(name)
        if treffer is None:
            return self._json(404, {"meldung": "Nicht gefunden"})
        try:
            inhalt = await run_in_threadpool(self._medium, name)
        except LearningWeg:
            return self._json(503, {"meldung": NICHT_ERREICHBAR})
        if inhalt is None:
            return self._json(404, {"meldung": "Nicht gefunden"})
        # Der Typ kommt aus der Endung, nicht von der Gegenseite.
        return Response(inhalt, media_type=_MEDIEN_ART[treffer.group(1)], headers={
            "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff",
        })

    def _medium(self, name: str) -> bytes | None:
        # Die Medienroute von learning-api ist offen; der Dienstschluessel geht nicht mit.
        try:
            antwort = self._http.get(f"{self._e.api_url.rstrip('/')}/lernsequenz-medien/{name}")
        except httpx.HTTPError as fehler:
            log.warning("learning-api nicht erreichbar: %s", _typ(fehler))
            raise LearningWeg("medien") from None
        if antwort.status_code == 404:
            return None
        if antwort.status_code != 200 or len(antwort.content) > MAX_MEDIUM:
            log.warning("learning-api Medien: HTTP %s", antwort.status_code)
            raise LearningWeg("medien")
        return antwort.content

    async def _r_notizen(self, request: Request) -> JSONResponse:
        course_id = request.query_params.get("course_id", "")
        return await self._ausfuehren(lambda: self._notizen(course_id), ablauf=False)

    def _notizen(self, course_id: str) -> dict[str, Any]:
        kurs = self._kurs(course_id)
        daten = self._api_json(self._api("GET", "/api/v1/notizen", params={"quelle": "bewertung"}))
        notizen = daten.get("notes")
        if not isinstance(notizen, list):
            raise LearningWeg("notes")
        return {"notes": [
            n for n in notizen
            if isinstance(n, dict) and _uuid(str(n.get("course_id"))) == kurs.course_id
        ]}

    async def _r_bewertung(self, request: Request) -> JSONResponse:
        course_id = request.path_params["course_id"]
        if request.method == "GET":
            return await self._ausfuehren(lambda: self._bewertung_lesen(course_id), ablauf=False)
        return await self._mit_body(request, lambda body: self._bewertung_abgeben(course_id, body), ablauf=False)

    def _bewertung_lesen(self, course_id: str) -> dict[str, Any]:
        kurs = self._kurs(course_id)
        antwort = self._api("GET", f"/api/v1/kursbewertung/{kurs.course_id}")
        if antwort.status_code == 404:
            raise _Antwort(404, {"meldung": UNBEKANNTER_KURS})
        return self._api_json(antwort)

    def _bewertung_abgeben(self, course_id: str, body: dict[str, Any]) -> dict[str, Any]:
        kurs = self._kurs(course_id)
        smiley, stichworte = body.get("smiley"), body.get("stichworte", [])
        if not isinstance(smiley, str) or smiley not in SMILEYS:
            raise _Antwort(400, {"code": "smiley_unbekannt", "meldung": SMILEY_UNBEKANNT})
        if not isinstance(stichworte, list) or not all(isinstance(w, str) for w in stichworte):
            raise _Antwort(400, {"code": "invalid_request", "meldung": UNGUELTIG})
        if len(stichworte) > 3:
            raise _Antwort(400, {"code": "zu_viele_stichworte", "meldung": ZU_VIELE_STICHWORTE})
        antwort = self._api("POST", "/api/v1/kursbewertung", json_body={
            "course_id": kurs.course_id,
            "kurs_titel": kurs.titel,
            "smiley": smiley,
            "stichworte": stichworte,
        })
        if antwort.status_code == 404:
            raise _Antwort(404, {"meldung": UNBEKANNTER_KURS})
        if antwort.status_code == 400:
            try:
                fehler = antwort.json()
            except ValueError:
                fehler = None
            code = fehler.get("code") if isinstance(fehler, dict) else None
            text = fehler.get("message") if isinstance(fehler, dict) else None
            raise _Antwort(400, {
                "code": str(code or "bewertung_ungueltig")[:60],
                "meldung": str(text or "Die Bewertung ist nicht gültig.")[:200],
            })
        return self._api_json(antwort)


__all__ = ["Bruecke", "rueckmeldung_text"]
