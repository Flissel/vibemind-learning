"""Bruecke der Lern-Huelle: JSON-API unter `/lernen/api/` zwischen Seite, Learning und PenEcho.

Starlette mit async Handlern; die Clients (LernMcp, PenEchoMcp, httpx.Client) sind
synchron und laufen im Threadpool. Routen, die Sitzung, Flaeche oder Sperre veraendern
(oeffnen, antwort, weiter, zeichnung, abgeben, korrektur, passt), laufen nacheinander unter
einer Sperre des Ablaufs; PenEchoMcp ist nicht fuer parallele Aufrufe gebaut. Seit es die
Flaechenspur gibt (Listenkurse: Aufgaben im Hintergrund setzen), laeuft zudem jeder Aufruf an die
Flaeche unter `_flaeche_sperre` (`_auf_flaeche`, `_board_merken`, die Arbeiten der Spur).

Listenkurse (Body mit `nummer`): `abgeben` nimmt das Bild synchron unter der Flaechensperre auf und reiht
die Vorbewertung in die Modellspur ein (ohne Sperre, ohne `_ablauf`), Antwort sofort 202. Die fertige
Karte setzt die Flaechenspur (vorrang 0). `_ablauf` umfasst nur noch kurze Learning-Buchungen (passt,
korrektur, oeffnen), nie einen Modellaufruf. `stand` liefert den Status je Aufgabe, `zeigen` holt eine
Aufgabe ins Bild.

Flaeche zuerst: `abgeben` (Schnappschuss -> Vorbewertung -> Karte), `korrektur` („Falsch
gelesen“/„Selbst eintippen“: getippte Antwort mit `korrigiert_aus`) und `passt` (Vorbewertung
buchen). „Ueberarbeiten“ braucht keine Route. Die Bruecke merkt sich keine Vorbewertung: ihre
Kennung kommt von der Seite, Learning prueft sie (auch nach einem Neustart der Huelle).

Die Lernenden-ID kommt nur aus der Einstellung. Geloggt werden nur Typnamen.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, NoReturn, TypeVar
from uuid import UUID, uuid5

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..contracts.beschriftung import beschriften
from .anordnung import ZuVieleAufgaben, anordnung
from .bericht import aufgaben as bericht_aufgaben, bericht_text
from .einstellung import Einstellung, Kurs
from .hintergrund import Spur
from .lern_mcp import LearningAbgelehnt, LearningWeg, LernMcp
from .penecho_mcp import (
    BEREICHE, BEREICH_BREITE, BEREICH_HOEHE, Aufgabenbild, PenEchoMcp, PenEchoNichtErreichbar,
    PenEchoSitzungUnbekannt,
)
from .sperre import Sperre
from .tafel import aufgabentext, leistentext
from .zusammenfassung import (
    BILD_BUDGET_S, AufgabeZeile, Zusammenfassung, als_markdown, als_pdf, ergebnis_von, kernaussage, kurs_kurz,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# Feste Texte (Plan, global.md).
NICHT_ERREICHBAR = "Learning ist gerade nicht erreichbar. Ihre Fläche bleibt erhalten."
SCHON_ABGEGEBEN = "Diese Aufgabe wurde schon abgegeben. Ich lade den aktuellen Stand."
NICHT_ANGENOMMEN = "Diese Abgabe wurde nicht angenommen. Bitte prüfen Sie Ihre Eingabe."  # Ruling H
KEIN_KURS = "Dieser Kurs hat noch keine freigegebenen Aufgaben."
ZU_VIELE_AUFGABEN = "Dieser Kurs hat mehr Aufgaben, als auf eine Fläche passen."
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
# Flaeche zuerst (Abgeben, Falsch gelesen, Passt).
KEINE_FLAECHENAUFGABE = "Diese Aufgabe beantworten Sie in der Seitenleiste."
KEINE_KORREKTUR = "Bei dieser Aufgabe gibt es nichts Gelesenes zu korrigieren."
BEWERTUNG_WEG = "Die Bewertung ist gerade nicht erreichbar. Bitte geben Sie gleich noch einmal ab."
NUR_UEBEN = "In der Hülle können Sie nur üben. Diese Sitzung lässt keine Vorbewertung zu."
NICHT_BESTAETIGT = (
    "Diese Rückmeldung lässt sich nicht bestätigen. Bitte überarbeiten Sie Ihr Ergebnis oder tippen Sie es selbst ein."
)
# Listenkurs: Abgaben im Hintergrund (409 mit code).
LAEUFT_SCHON = "Diese Aufgabe wird gerade bewertet."
SCHON_BESTAETIGT = "Diese Aufgabe ist schon bestätigt."
NICHT_IM_LISTENMODUS = "Bitte geben Sie diese Aufgabe über „Abgeben“ ab."
# Text der Rueckmeldungskarte unter dem Urteil (nur Richtigkeit, nie die Zahl der Versuche).
KARTE_RICHTIG = "Gut gemacht."
KARTE_NOCH_NICHT = (
    "Sie können Ihr Ergebnis überarbeiten und noch einmal abgeben. Die KI auf der Fläche erklärt Ihnen gern den Weg."
)
KARTE_NICHT_LESBAR = (
    "Ihr Ergebnis war nicht sicher lesbar. Bitte schreiben Sie es deutlicher in das Feld oder tippen Sie es selbst ein."
)

# Wie services/adaptive_engine/session_service.py: deterministisch bewertete Typen lesen das
# Ergebnisfeld, Rubrik-Typen den ganzen Aufgabenbereich. `code` bleibt in der Seitenleiste.
DETERMINISTISCHE_TYPEN = frozenset({"single_choice", "multiple_choice", "matching", "ordering", "numeric", "fill_in"})
BEREICHS_TYPEN = frozenset({"open", "case", "penecho_canvas"})
_FLAECHEN_TYPEN = DETERMINISTISCHE_TYPEN | BEREICHS_TYPEN
_BEWERTUNG_WEG_CODE = "session_grading_unavailable"

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
    board: str | None = None        # PenEcho-Sitzung, mit der zuletzt ein Flaechenaufruf gelang
    # Gespraech mit dem Tutor zur aktuellen Aufgabe: (Frage, Antwort); der Tutor selbst merkt sich nichts.
    tutor: list[tuple[str, str]] = field(default_factory=list)
    tutor_aufgabe: str | None = None
    bericht_gezeigt: bool = False   # Bericht dieser Sitzung steht auf dem Board
    liste: bool = False             # Listenkurs (Turn mit `ablauf == "liste"`): Routen mit `nummer`


@dataclass(frozen=True)
class _Abgabe:
    """Abgabestand einer Listenaufgabe, nur im Speicher; nach einem Neustart zaehlt allein Learning."""

    status: str                     # "in_bewertung" | "fehler"
    meldung: str | None = None      # nur bei "fehler": Text fuer die Lernenden


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


def _sperrt(turn: dict[str, Any]) -> bool:
    """Nur eine offene Pruefungsaufgabe schliesst die KI; beim Ueben hilft sie jederzeit (User-Wahl 06.11.).

    Ohne lesbaren Modus zaehlt die Aufgabe als Pruefung.
    """
    return _task(turn) is not None and turn.get("mode") != "training"


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


def _hat_bilder(task: dict[str, Any]) -> bool:
    """Ob die Aufgabe Bilder hat, aus denselben Eintraegen wie `Bruecke._aufgabenbilder`, ohne sie zu laden."""
    presentation = task.get("presentation")
    if not isinstance(presentation, dict):
        return False
    eintraege = [presentation]
    for seite in ("left", "right"):
        liste = presentation.get(seite)
        eintraege += [e for e in (liste if isinstance(liste, list) else []) if isinstance(e, dict)]
    return any(_bildname(e.get("image")) is not None for e in eintraege)


def _gross(task: dict[str, Any]) -> bool:
    """Hoher Bereich (anordnung.HOEHE_GROSS): Aufgaben mit Bildern und Zeichenaufgaben."""
    return task.get("type") == "penecho_canvas" or _hat_bilder(task)


_LISTEN_STATUS = frozenset({"offen", "bestaetigt"})


def _listeneintraege(result: dict[str, Any]) -> list[dict[str, Any]]:
    """`aufgaben` aus `learning_task_list`, geprueft: Nummern 1..n lueckenlos, Bildfelder umgeschrieben."""
    roh = result.get("aufgaben")
    if not isinstance(roh, list) or not roh:
        raise LearningWeg("aufgaben")
    eintraege = []
    for e in roh:
        nummer = e.get("nummer") if isinstance(e, dict) else None
        task = e.get("task") if isinstance(e, dict) else None
        if (isinstance(nummer, bool) or not isinstance(nummer, int) or not isinstance(task, dict)
                or not isinstance(task.get("id"), str) or e.get("status") not in _LISTEN_STATUS
                or not isinstance(e.get("attempt_id"), str) or not e["attempt_id"]
                or not isinstance(e.get("vorbewertung"), (dict, type(None)))):
            raise LearningWeg("aufgaben")
        _medien({"task": task})
        eintraege.append(e)
    eintraege.sort(key=lambda e: e["nummer"])
    if [e["nummer"] for e in eintraege] != list(range(1, len(eintraege) + 1)):
        raise LearningWeg("aufgaben")
    return eintraege


def _naechste_offene(aufgaben: list[dict[str, Any]], ab: int) -> int | None:
    """Erste Nummer nach `ab` mit Status != bestaetigt, am Ende von vorn (wie `liste.js` naechsteOffene)."""
    n = len(aufgaben)
    for schritt in range(1, n + 1):
        a = aufgaben[(ab - 1 + schritt) % n]
        if a["status"] != "bestaetigt":
            return a["nummer"]
    return None


def _ablehnungstext(code: str) -> str:
    """Text zu einer Ablehnung der Vorbewertung, wie `_vorbewertung_abgelehnt`/`_stand_neu` (Ruling H)."""
    if code == "session_access_denied":
        return NUR_UEBEN
    return SCHON_ABGEGEBEN if code in _SCHON_ABGEGEBEN_CODES else NICHT_ANGENOMMEN


def _bildname(wert: object) -> str | None:
    """Name eines Aufgabenbilds auf der eigenen Route (nach `_medien`); sonst None."""
    if not isinstance(wert, str) or not wert.startswith(MEDIEN_ROUTE):
        return None
    name = wert.removeprefix(MEDIEN_ROUTE)
    return name if MEDIEN_NAME.fullmatch(name) else None


def _seitenverhaeltnis(eintrag: dict[str, Any]) -> float:
    breite, hoehe = eintrag.get("width"), eintrag.get("height")
    zahlen = all(isinstance(z, (int, float)) and not isinstance(z, bool) and z > 0 for z in (breite, hoehe))
    return breite / hoehe if zahlen else 4 / 3  # type: ignore[operator]


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
    zeilen += _kriterien_zeilen(feedback)
    return "\n\n".join(zeilen) or None


def _kriterien_zeilen(feedback: dict[str, Any]) -> list[str]:
    """Die nicht voll erfuellten Kriterien als „Beschreibung: Rueckmeldung“, ohne Quellenmarken."""
    zeilen = []
    for k in feedback.get("criteria") or []:
        if not isinstance(k, dict):
            continue
        score, text = k.get("score"), k.get("feedback")
        if isinstance(score, (int, float)) and score < 1 and isinstance(text, str) and text.strip():
            beschreibung = k.get("description")
            beschreibung = beschreibung.rstrip(" .:") if isinstance(beschreibung, str) else ""
            text = _ohne_quellenmarken(text)
            zeilen.append(f"{beschreibung}: {text}" if beschreibung else text)
    return zeilen


# Learnings Urteil (`session_service._turn_feedback`: correct / partial / incorrect / needs_review)
# auf die Karte; so sagt die Karte dasselbe wie der Turn nach „Passt“.
_URTEIL_AUS_ZUSTAND = {
    "correct": "Richtig.",
    "partial": "Teilweise richtig.",
    "incorrect": "Noch nicht richtig.",
    "needs_review": "Wird noch geprüft.",  # Rubrik-Bewertung nicht angenommen: ein Mensch prueft
}
_ZUSTANDSNAME = re.compile(r"^[a-z_]{1,40}$")


def urteil_aus(vorbewertung: dict[str, Any]) -> str:
    """Urteil der Rueckmeldungskarte, vollstaendig aus Learnings `feedback.state`; keine eigene Schwelle.

    Nicht lesbar -> „Nicht lesbar.“. Nur ein fehlender oder hier unbekannter Zustand ergibt
    „Noch nicht richtig.“; geloggt wird dann nur der Zustandsname.
    """
    if vorbewertung.get("lesbar") is not True:
        return "Nicht lesbar."
    feedback = vorbewertung.get("feedback")
    zustand = feedback.get("state") if isinstance(feedback, dict) else None
    urteil = _URTEIL_AUS_ZUSTAND.get(zustand) if isinstance(zustand, str) else None
    if urteil is None:
        name = zustand if isinstance(zustand, str) and _ZUSTANDSNAME.fullmatch(zustand) else type(zustand).__name__
        log.warning("Vorbewertung ohne bekanntes Urteil: %s", name)
        return "Noch nicht richtig."
    return urteil


def kartentext(vorbewertung: dict[str, Any]) -> str:
    """Rueckmeldung unter dem Urteil: Kriterien (Rubrik), sonst ein kurzer Satz je Urteil."""
    urteil = urteil_aus(vorbewertung)
    if urteil == "Nicht lesbar.":
        return KARTE_NICHT_LESBAR
    feedback = vorbewertung.get("feedback")
    zeilen = _kriterien_zeilen(feedback) if isinstance(feedback, dict) else []
    if urteil == "Richtig.":
        zeilen.append(KARTE_RICHTIG)
    elif urteil == "Wird noch geprüft.":
        zeilen.append(WIRD_GEPRUEFT)  # derselbe Satz wie im Turn nach „Passt“
    else:
        zeilen.append(KARTE_NOCH_NICHT)
    return "\n\n".join(zeilen)


def _kartenteile(vorbewertung: dict[str, Any]) -> tuple[str | None, str, str]:
    """(Gelesenes, Urteil, Text) der Rueckmeldungskarte; ohne Gelesenes, wenn nicht lesbar."""
    urteil = urteil_aus(vorbewertung)
    gelesen = vorbewertung.get("gelesen_text") if urteil != "Nicht lesbar." else None
    text = kartentext(vorbewertung)
    return (gelesen if isinstance(gelesen, str) else None), urteil, text


# mcp/tools/tutor.py nummeriert die Quellen als S1, S2, ...; die Huelle zeigt sie nicht an.
_QUELLENMARKE = re.compile(
    r"\s*\((?:vgl\.\s*|siehe\s+)?S\d+(?:\s*,\s*S\d+)*\)"  # (S1), (vgl. S1, S2), (siehe S2)
    r"|\s+(?:aus|in|nach|siehe)\s+S\d+\b"                 # "die pq-Formel aus S2"
)


def _ohne_quellenmarken(text: str) -> str:
    return _QUELLENMARKE.sub("", text)


TUTOR_VERLAUF = 3  # so viele Wechsel gehen bei einer Rueckfrage mit
_TUTOR_ANTWORT_MAX = 2000


def _mit_verlauf(verlauf: list[tuple[str, str]], frage: str) -> str:
    """Rueckfrage samt bisherigem Gespraech; Learnings Tutor sieht sonst nur die einzelne Frage."""
    if not verlauf:
        return frage
    zeilen = ["Bisheriges Gespräch zu dieser Aufgabe:"]
    for alt, antwort in verlauf[-TUTOR_VERLAUF:]:
        zeilen += [f"Lernende Person: {alt}", f"Tutor: {antwort[:_TUTOR_ANTWORT_MAX]}"]
    zeilen += ["", f"Neue Frage der lernenden Person: {frage}"]
    return "\n".join(zeilen)


def _tafeltext(task: dict[str, Any], nummer: int) -> str:
    """`tafel.aufgabentext` ohne den Kopf „Aufgabe {nummer}: “, den `aufgabe_setzen` selbst voranstellt.

    So steht auf dem Board genau `aufgabentext(task, nummer)` und der Kopf nicht doppelt.
    """
    return aufgabentext(task, nummer).removeprefix(f"Aufgabe {nummer}: ")


def _revision_aus(body: dict[str, Any]) -> int:
    revision = body.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise _Antwort(400, {"meldung": UNGUELTIG})
    return revision


def _vorbewertung_id_aus(body: dict[str, Any]) -> str:
    """Kennung der Vorbewertung aus dem Body, als normierte UUID (Learning vergibt uuid4)."""
    kennung = body.get("vorbewertung_id")
    norm = _uuid(kennung) if isinstance(kennung, str) and len(kennung) <= 64 else None
    if norm is None:
        raise _Antwort(400, {"meldung": UNGUELTIG})
    return norm


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
        kursende: Path | None = None,
    ) -> None:
        self._e = einstellung
        self._lern = lern
        self._penecho = penecho
        self._sperre = sperre
        self._http = http
        self._kurse = {k.course_id: k for k in einstellung.kurse}
        self._sitzungen: dict[str, _Sitzung] = {}
        # Learnings Ueberblick kennt nur aktive Sitzungen. Damit ein Neuladen nach dem Kursende
        # nicht stumm neu startet, merkt sich die Huelle den abgeschlossenen Durchlauf
        # ("Kennung|Kurs" -> Sitzung); ohne Datei nur im Speicher (Tests).
        self._kursende_datei = kursende
        self._kursende = self._kursende_lesen()
        self._ablauf = asyncio.Lock()
        # PenEchoMcp ist nicht fuer parallele Aufrufe gebaut: jeder Flaechenaufruf (Handler wie Spur) laeuft
        # unter dieser Sperre. Reentrant, weil ein Handler verschachtelte Flaechenhelfer ruft.
        self._flaeche_sperre = threading.RLock()
        # Listenkurse: die uebrigen Aufgaben kommen nacheinander im Hintergrund auf die Flaeche.
        self._flaechenspur = Spur("flaeche", sperre=self._flaeche_sperre)  # type: ignore[arg-type]
        # Listenkurse: die Vorbewertung einer Abgabe (Modellaufruf) laeuft hier, nie unter einer Sperre.
        self._modellspur = Spur("modell")
        self._spuren = [self._flaechenspur, self._modellspur]
        # Die zuletzt geoeffnete Lernsitzung: nur deren Hintergrundarbeiten laufen noch (Kurswechsel).
        self._zuletzt: str | None = None
        # Uhr fuer das Bildbudget der Zusammenfassung (Tests ersetzen sie).
        self._uhr: Callable[[], float] = time.monotonic
        # Abgaben der Listenkurse, je (sid, nummer); eigene kleine Sperre, nie die Flaechensperre.
        self._abgaben_sperre = threading.Lock()
        self._abgaben: dict[tuple[str, int], _Abgabe] = {}
        # Fertige Vorbewertung, deren Karte noch auf die Flaeche muss: (task, vorbewertung); die Arbeit der
        # Flaechenspur nimmt immer die neueste.
        self._karten: dict[tuple[str, int], tuple[dict[str, Any], dict[str, Any]]] = {}
        # Zaehlt je Nummer die frischen Karten; eine aeltere setzen-Arbeit setzt dann ihre alte Karte nicht.
        self._kartenstand: dict[tuple[str, int], int] = {}
        # Karte, die PenEcho nicht aufnahm (Platz belegt, Aufruf gescheitert): ihr Text fuer die Leiste, je Nummer.
        self._kartentexte: dict[tuple[str, int], str] = {}
        p = "/lernen/api"
        self._app = Starlette(routes=[
            Route(f"{p}/kurse", self._r_kurse, methods=["GET"]),
            Route(f"{p}/kurse/{{course_id}}/oeffnen", self._r_oeffnen, methods=["POST"]),
            Route(f"{p}/kurse/{{course_id}}/neu", self._r_neu, methods=["POST"]),
            Route(f"{p}/kurse/{{course_id}}/bewertung", self._r_bewertung, methods=["GET", "POST"]),
            Route(f"{p}/sitzungen/{{sid}}/antwort", self._r_antwort, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/weiter", self._r_weiter, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/zeichnung", self._r_zeichnung, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/abgeben", self._r_abgeben, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/korrektur", self._r_korrektur, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/passt", self._r_passt, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/stand", self._r_stand, methods=["GET"]),
            Route(f"{p}/sitzungen/{{sid}}/zusammenfassung.pdf", self._r_zusammenfassung, methods=["GET"]),
            Route(f"{p}/sitzungen/{{sid}}/zeigen", self._r_zeigen, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/erklaeren", self._r_erklaeren, methods=["POST"]),
            Route(f"{p}/sitzungen/{{sid}}/hinweis", self._r_hinweis, methods=["POST"]),
            Route(f"{p}/notizen", self._r_notizen, methods=["GET"]),
            Route(f"{p}/medien/{{name}}", self._r_medien, methods=["GET"]),
        ])

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self._app(scope, receive, send)

    def beenden(self) -> None:
        """Spuren anhalten (Herunterfahren): Wartendes verfaellt, eine laufende Arbeit endet noch."""
        for spur in self._spuren:
            spur.beenden()

    # ---- Rahmen -----------------------------------------------------------
    @staticmethod
    def _json(status: int, body: Any) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    async def _ausfuehren(self, arbeit: Callable[[], Any], *, ablauf: bool, status: int = 200) -> JSONResponse:
        try:
            if ablauf:
                async with self._ablauf:
                    body = await run_in_threadpool(arbeit)
            else:
                body = await run_in_threadpool(arbeit)
        except _Antwort as antwort:
            return self._json(antwort.status, antwort.body)
        except LearningWeg as fehler:
            # Die Sperre bleibt, wie sie ist.
            if fehler.code == _BEWERTUNG_WEG_CODE:
                # Leser oder Rubrik gerade nicht verfuegbar; nichts wurde gespeichert, erneut abgeben geht.
                return self._json(503, {"meldung": BEWERTUNG_WEG, "code": fehler.code})
            return self._json(503, {"meldung": NICHT_ERREICHBAR})
        except LearningAbgelehnt as fehler:
            return self._json(409, {"meldung": fehler.nachricht, "code": fehler.code})
        return self._json(status, body)

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

    # ---- Kursende ---------------------------------------------------------
    def _kursende_lesen(self) -> dict[str, str]:
        if self._kursende_datei is None:
            return {}
        try:
            wert = json.loads(self._kursende_datei.read_text(encoding="utf-8"))
            return {k: v for k, v in wert.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _kursende_schreiben(self) -> None:
        if self._kursende_datei is None:
            return
        ziel = self._kursende_datei
        try:
            ziel.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=ziel.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self._kursende, f)
            os.replace(tmp, ziel)
        except OSError as fehler:
            log.warning("Kursende nicht gespeichert: %s", _typ(fehler))

    def _ende_schluessel(self, kurs: Kurs) -> str:
        return f"{self._e.actor_id}|{kurs.course_id}"

    def _ende_setzen(self, kurs: Kurs, sid: str | None) -> None:
        schluessel = self._ende_schluessel(kurs)
        if self._kursende.get(schluessel) == sid:
            return
        if sid is None:
            self._kursende.pop(schluessel, None)
        else:
            self._kursende[schluessel] = sid
        self._kursende_schreiben()

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
        liste = turn.get("ablauf") == "liste"
        # Im Listenkurs kommt die Nummer nur aus der Liste (oeffnen, weiter, passt), nie aus `completed + 1`.
        sitzung = _Sitzung(kurs, norm, task["id"] if task else None,
                           _nummer(turn) if task and not liste else None, liste=liste)
        self._sitzungen[norm] = sitzung
        if self._zuletzt is None:
            # Nach einem Neustart der Huelle ist die erste Sitzung, mit der die Seite spricht, die offene;
            # sonst kaemen die Karten der Hintergrund-Abgaben erst nach einem erneuten Oeffnen.
            self._zuletzt = norm
        return norm, sitzung, turn

    # ---- PenEcho ----------------------------------------------------------
    def _auf_flaeche(self, sitzung: _Sitzung, aktion: Callable[[str], T]) -> T:
        """Canvas-Aufruf auf der Flaeche dieser Lernsitzung (Ruling L).

        Kennt PenEcho die Sitzung nicht mehr, einmal neu anlegen und wiederholen. Alles unter der
        Flaechensperre, auch die Wahl der Sitzung.
        """
        titel = f"{sitzung.kurs.titel} · {date.today():%d.%m.%Y}"
        with self._flaeche_sperre:
            session_id = self._penecho.sitzung(sitzung.sid, titel)
            try:
                ergebnis = aktion(session_id)
            except PenEchoSitzungUnbekannt:
                sitzung.board = None
                self._penecho.vergessen(sitzung.sid)
                session_id = self._penecho.sitzung(sitzung.sid, titel)
                ergebnis = aktion(session_id)
            sitzung.board = session_id
        return ergebnis

    def _flaeche_still(self, sitzung: _Sitzung, aktion: Callable[[str], Any]) -> bool:
        try:
            self._auf_flaeche(sitzung, aktion)
        except (PenEchoNichtErreichbar, OSError, ValueError) as fehler:
            # ValueError: Aufgabennummer ausserhalb der 20 Bereiche der Flaeche.
            log.warning("Flaeche uebersprungen: %s", _typ(fehler))
            return False
        return True

    def _zeigen(self, sitzung: _Sitzung, turn: dict[str, Any]) -> tuple[int | None, bool]:
        """Aufgabe auf die Flaeche setzen; eine Pruefungsaufgabe sperrt die KI, sonst wird die Sperre geloest."""
        task = _task(turn)
        if task is None:
            sitzung.aufgabe_id, sitzung.nummer, sitzung.auf_flaeche = None, None, False
            if turn.get("state") == "completed":
                self._ende_setzen(sitzung.kurs, sitzung.sid)
                self._bericht(sitzung, turn, self._bericht_rechtecke(sitzung, turn))
            self._frei(sitzung)
            return None, False
        nummer = _nummer(turn)
        aufgabe_id = task["id"]
        if _sperrt(turn):
            self._sperre.offen(aufgabe_id)
        else:
            self._frei(sitzung)
        if sitzung.aufgabe_id == aufgabe_id and sitzung.nummer == nummer and sitzung.auf_flaeche:
            return nummer, True
        ok = self._flaeche_still(sitzung, self._setzauftrag(task, nummer))
        sitzung.aufgabe_id, sitzung.nummer, sitzung.auf_flaeche = aufgabe_id, nummer, ok
        return nummer, ok

    def _setzauftrag(
        self, task: dict[str, Any], nummer: int, rechteck: dict[str, int] | None = None, *, zeigen: bool = True,
    ) -> Callable[[str], None]:
        """`aufgabe_setzen` fuer eine Flaechensitzung; Text und Bilder werden hier einmal vorbereitet.

        Ohne Rechteck genau der Aufruf wie vor den Listenkursen (fester Bereich `bereich(nummer)`).
        `zeigen=False` fuer das Setzen im Hintergrund: kein show, das Board bleibt, wie es ist.
        """
        typ = task.get("type")
        text = str(task.get("prompt") or "") if typ == "code" else _tafeltext(task, nummer)
        feld, arbeit = typ in DETERMINISTISCHE_TYPEN, typ in BEREICHS_TYPEN
        bilder = self._aufgabenbilder(task)
        lage: dict[str, Any] = {} if rechteck is None else {"rechteck": rechteck}
        if not zeigen:
            lage["zeigen"] = False
        return lambda s: self._penecho.aufgabe_setzen(
            s, task["id"], text, nummer, mit_ergebnisfeld=feld, mit_arbeitsfeld=arbeit, bilder=bilder, **lage)

    def _board_merken(self, sitzung: _Sitzung) -> None:
        """documentId des Boards merken (`dokument_merken`), still: Fehler nur als Typ geloggt, nie gemeldet.

        Das eigentliche Speichern in PenEcho loest die Seite aus (BOARD_SPEICHERN.md); der Server kann es nicht.
        Nur fuer ein Board, mit dem diese Bruecke schon gearbeitet hat: kein `sitzung()`, kein Neuanlegen,
        kein Wiederholen (etwa nach einem Neustart der Huelle bei `passt`; `weiter` merkt es danach).
        `dokument_merken` selbst fragt PenEcho nur per `penecho_inspect_session`.
        """
        if sitzung.board is None:
            return
        try:
            with self._flaeche_sperre:
                self._penecho.dokument_merken(sitzung.board)
        except Exception as fehler:  # noqa: BLE001 - Merken darf nie eine Abgabe aufhalten
            log.warning("Board nicht gemerkt: %s", _typ(fehler))

    def _frei(self, sitzung: _Sitzung) -> None:
        """Die Sperre ist global: nur loesen, wenn keine andere Sitzung eine offene Pruefungsaufgabe hat.

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
            neu = task["id"] if task else None
            if andere.liste:
                andere.aufgabe_id = neu  # Listennummer bleibt, wie die Liste sie gesetzt hat
            else:
                nummer = _nummer(turn) if task else None
                if neu != andere.aufgabe_id or nummer != andere.nummer:
                    andere.auf_flaeche = False
                andere.aufgabe_id, andere.nummer = neu, nummer
            if task is not None and _sperrt(turn):
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
                    liste = laufend.get("ablauf") == "liste"
                    self._sitzungen.setdefault(andere_sid, _Sitzung(
                        kurs, andere_sid, task["id"], None if liste else _nummer(laufend), liste=liste))
                if _sperrt(laufend):
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
            sitzung, lambda s: self._penecho.rueckmeldung_setzen(s, aufgabe_id, nummer, titel, text)
        )
        return text, ok

    def _bericht(
        self, sitzung: _Sitzung, turn: dict[str, Any], rechtecke: dict[int, dict[str, int]] | None = None,
    ) -> None:
        """Berichtskarte am Kursende auf das Board; hoechstens einmal je Sitzung, nie ein Abbruch.

        Feste Kennungen (Karte `bericht-{sid}`, Bild `bericht-{sid}-{nummer}`) machen auch ein erneutes
        Oeffnen nach einem Neustart der Bruecke wiederholbar. Die Kursende-Seite bleibt die letzte Seite.
        `rechtecke` (Listenkurs): Bereich je Nummer; dann gelten diese Nummern statt der festen 1..20.
        """
        summary = turn.get("summary")
        if sitzung.bericht_gezeigt or not isinstance(summary, dict):
            return
        if sitzung.liste and self._e.zusammenfassung_ordner:
            self._zusammenfassung_ablegen(sitzung)
        try:
            text = bericht_text(sitzung.kurs.titel, summary, self._begriffe(sitzung.kurs) or [])
            if rechtecke is None:
                nummern = tuple(n for n, _ in bericht_aufgaben(summary) if 1 <= n <= BEREICHE)
                lage: dict[str, Any] = {}
            else:
                nummern = tuple(n for n, _ in bericht_aufgaben(summary) if n in rechtecke)
                lage = {"rechteck": rechtecke}
            sitzung.bericht_gezeigt = self._flaeche_still(
                sitzung, lambda s: self._penecho.bericht_zeigen(s, sitzung.sid, text, nummern, **lage)
            )
        except Exception as fehler:  # noqa: BLE001 - der Bericht darf das Kursende nie blockieren
            log.warning("Bericht nicht gezeigt: %s", _typ(fehler))

    def _bericht_rechtecke(self, sitzung: _Sitzung, turn: dict[str, Any]) -> dict[int, dict[str, int]] | None:
        """Listenkurs: Rechteck je Nummer fuer den Bericht (aus der Liste); sonst oder ohne Liste None.

        Ohne lesbare Liste kein Bericht am alten Raster: dann ein leeres Mapping (nur die Karte).
        """
        if not (sitzung.liste or turn.get("ablauf") == "liste") or sitzung.bericht_gezeigt:
            return None
        try:
            _, eintraege = self._liste_lesen(sitzung)
            bereiche = self._bereiche(eintraege)
        except (LearningWeg, LearningAbgelehnt, _Antwort) as fehler:
            log.warning("Bericht ohne Rechtecke: %s", _typ(fehler))
            return {}
        return {e["nummer"]: bereiche[e["nummer"] - 1] for e in eintraege}

    def _begriffe(self, kurs: Any) -> list[Any] | None:
        """Aktuelle Beherrschung nach einer Abgabe; None, wenn Learning sie gerade nicht liefert."""
        try:
            ueberblick = self._lern.rufe("learning_course_overview", "learning.course.overview", course_id=kurs.course_id)
        except (LearningWeg, LearningAbgelehnt) as fehler:
            log.warning("Begriffe nach Abgabe nicht geladen: %s", _typ(fehler))
            return None
        begriffe = ueberblick.get("begriffe")
        return begriffe if isinstance(begriffe, list) else None

    # ---- Zusammenfassung (Listenkurs) ---------------------------------------
    def _zusammenfassung_bauen(self, sitzung: _Sitzung, *, bilder: bool) -> Zusammenfassung:
        """Werte der Zusammenfassung aus der Liste bei Learning; Bilder (Flaeche, je Aufgabe) nur mit `bilder`."""
        _, eintraege = self._liste_lesen(sitzung)
        bereiche = self._bereiche(eintraege)
        aufnahmen: list[bytes | None] = [None] * (len(eintraege) + 1)
        if bilder:
            ganz = {
                "x": min(b["x"] for b in bereiche), "y": min(b["y"] for b in bereiche),
                "w": max(b["x"] + b["w"] for b in bereiche) - min(b["x"] for b in bereiche),
                "h": max(b["y"] + b["h"] for b in bereiche) - min(b["y"] for b in bereiche),
            }
            # Erst die Aufgaben, die ganze Flaeche zuletzt (langsamste, heikelste Aufnahme).
            bilder_roh = self._aufnahmen(sitzung, [*bereiche, ganz])
            aufnahmen = [bilder_roh[-1], *bilder_roh[:-1]]
        zeilen = []
        for e, bild in zip(eintraege, aufnahmen[1:]):
            vor = e.get("vorbewertung")
            prompt = e["task"].get("prompt")
            zeilen.append(AufgabeZeile(
                e["nummer"], str(prompt) if prompt else "", ergebnis_von(e["status"], vor),
                kernaussage(vor) if isinstance(vor, dict) else "–", bild,
            ))
        return Zusammenfassung(sitzung.kurs.titel, date.today(), aufnahmen[0], tuple(zeilen),
                               self._begriffe_zeilen(sitzung.kurs))

    @staticmethod
    def _aufnahme_fehler_einzeln(fehler: Exception) -> bool:
        """Ein Werkzeugfehler von PenEcho (z. B. Aufnahme-Zeitueberschreitung) betrifft nur dieses eine Bild."""
        return type(fehler) is PenEchoNichtErreichbar and fehler.args in (("werkzeug",), ("bild",))

    def _aufnahmen(self, sitzung: _Sitzung, rechtecke: list[dict[str, int]]) -> list[bytes | None]:
        """Ein Bild je Rechteck in dieser Reihenfolge; das Zeitbudget gilt fuer alle zusammen.

        Nur auf dem gemerkten Board der zuletzt geoeffneten Sitzung: ein Lesezugriff oeffnet oder legt nie ein
        Board an und wiederholt nie. Jede Aufnahme bekommt den Rest des Budgets als Zeitlimit, jede liegt kurz
        unter der Flaechensperre. Ein Werkzeugfehler laesst nur dieses Bild weg; ein Verbindungsfehler oder ein
        aufgebrauchtes Budget beendet die Aufnahmen.
        """
        ergebnis: list[bytes | None] = [None] * len(rechtecke)
        start = self._uhr()
        for i, rechteck in enumerate(rechtecke):
            rest = BILD_BUDGET_S - (self._uhr() - start)
            if rest <= 0:
                log.warning("Zusammenfassung: Zeitbudget fuer Bilder aufgebraucht")
                break
            try:
                with self._flaeche_sperre:
                    if not self._noch_offen(sitzung) or not sitzung.board:
                        log.info("Zusammenfassung: kein gemerktes Board, keine Bilder")
                        break
                    ergebnis[i] = self._penecho.schnappschuss(
                        sitzung.board, i + 1, rechteck=rechteck, timeout=rest)
            except Exception as fehler:  # noqa: BLE001 - ohne Bild gibt es das PDF trotzdem
                log.warning("Zusammenfassung: Bild nicht aufgenommen: %s", _typ(fehler))
                if not self._aufnahme_fehler_einzeln(fehler):
                    break
        return ergebnis

    def _begriffe_zeilen(self, kurs: Any) -> tuple[tuple[str, str], ...]:
        zeilen = []
        for begriff in self._begriffe(kurs) or []:
            titel = begriff.get("titel") if isinstance(begriff, dict) else None
            if not isinstance(titel, str) or not titel:
                continue
            stand = begriff.get("beherrschung")
            ok = isinstance(stand, (int, float)) and not isinstance(stand, bool)
            zeilen.append((titel, f"{round(min(max(float(stand), 0.0), 1.0) * 100)} %" if ok else "noch offen"))
        return tuple(zeilen)

    def _zusammenfassung_ablegen(self, sitzung: _Sitzung, z: Zusammenfassung | None = None) -> None:
        """Textfassung in den Wissensordner (falls eingestellt); Fehler werden nur geloggt, nie gemeldet."""
        ordner = self._e.zusammenfassung_ordner
        if not ordner:
            return
        try:
            z = z or self._zusammenfassung_bauen(sitzung, bilder=False)
            ziel = Path(ordner) / f"{z.datum:%Y-%m-%d}-{kurs_kurz(z.kurs)}.md"
            ziel.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=ziel.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(als_markdown(z))
                os.replace(tmp, ziel)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except Exception as fehler:  # noqa: BLE001 - Kursende und PDF duerfen daran nie scheitern
            log.warning("Zusammenfassung nicht abgelegt: %s", _typ(fehler))

    async def _r_zusammenfassung(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        pdf: list[tuple[bytes, str]] = []

        def arbeit() -> dict[str, Any]:
            sitzung = self._listensitzung(sid)
            z = self._zusammenfassung_bauen(sitzung, bilder=True)
            pdf.append((als_pdf(z), f"zusammenfassung-{kurs_kurz(z.kurs)}-{z.datum:%Y-%m-%d}.pdf"))
            self._zusammenfassung_ablegen(sitzung, z)
            return {}

        antwort = await self._ausfuehren(arbeit, ablauf=False)
        if antwort.status_code != 200 or not pdf:
            return antwort
        inhalt, name = pdf[0]
        return Response(inhalt, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store",
        })

    def _stand_neu(self, sid: str, sitzung: _Sitzung, code: str) -> NoReturn:
        """Nach einer Ablehnung den aktuellen Stand laden; 409 mit Turn (Ruling H fuer den Text)."""
        turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=sid))
        if sitzung.liste:
            # Alter Weg in einem Listenkurs: nichts aus `completed + 1` vormerken (die Liste setzt die Nummer).
            task = _task(turn)
            sitzung.aufgabe_id = task["id"] if task else None
        else:
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

    async def _r_neu(self, request: Request) -> JSONResponse:
        course_id = request.path_params["course_id"]
        return await self._ausfuehren(lambda: self._oeffnen(course_id, neu=True), ablauf=True)

    def _abgeschlossen(self, kurs: Kurs) -> tuple[str, dict[str, Any]] | None:
        """Der gemerkte abgeschlossene Durchlauf, wenn Learning ihn noch als beendet zeigt."""
        sid = self._kursende.get(self._ende_schluessel(kurs))
        if sid is None:
            return None
        try:
            turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=sid))
        except LearningAbgelehnt as fehler:
            log.warning("Gemerktes Kursende abgelehnt, vergessen: %s", _typ(fehler))
            self._ende_setzen(kurs, None)
            return None
        if _task(turn) is not None or turn.get("state") != "completed":
            self._ende_setzen(kurs, None)
            return None
        return sid, turn

    def _oeffnen(self, course_id: str, *, neu: bool = False) -> dict[str, Any]:
        kurs = self._kurs(course_id)
        ueberblick = self._lern.rufe("learning_course_overview", "learning.course.overview", course_id=kurs.course_id)
        revision = ueberblick.get("course_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise _Antwort(409, {"meldung": KEIN_KURS})
        laufend = ueberblick.get("session")
        sid = _uuid(str(laufend.get("session_id"))) if isinstance(laufend, dict) else None
        if neu:
            self._ende_setzen(kurs, None)
        elif sid is None and (ende := self._abgeschlossen(kurs)) is not None:
            # Kursende: den beendeten Durchlauf zeigen (Bewertung), nichts neu starten.
            ende_sid, turn = ende
            self._zuletzt = ende_sid
            sitzung = self._sitzungen.setdefault(ende_sid, _Sitzung(kurs, ende_sid, liste=turn.get("ablauf") == "liste"))
            self._zeigen(sitzung, turn)
            begriffe = ueberblick.get("begriffe")
            beendet = {
                "kurs": {"course_id": kurs.course_id, "titel": kurs.titel},
                "begriffe": begriffe if isinstance(begriffe, list) else [],
                "turn": turn,
                "nummer": None,
                "flaeche": True,
                "abgeschlossen": True,
            }
            if sitzung.liste:
                # Die Seite zeigt den Link auf die Zusammenfassung ("jederzeit auf Wunsch"), nur dafuer das Kennzeichen.
                beendet["stand"] = {"ablauf": "liste"}
            return beendet
        if sid is None:
            gestartet = _turn(self._lern.rufe(
                "learning_session_start", "learning.session.start", course_id=kurs.course_id, schreibend=True,
                # Teil A2: neue Durchlaeufe sind feste Listen; fortgesetzte behalten, was Learning meldet.
                payload={"course_revision": revision, "mode": "training", "blueprint": dict(TRAINING_BLUEPRINT),
                         "ablauf": "liste"},
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
        self._zuletzt = sid  # Hintergrundarbeiten frueherer Sitzungen entfallen ab hier
        sitzung.liste = turn.get("ablauf") == "liste"
        liste: dict[str, Any] = {}
        # Ein Turn ohne `ablauf` (alte Sitzung) laeuft anpassend wie bisher.
        if sitzung.liste and _task(turn) is not None:
            nummer, flaeche, liste = self._liste_oeffnen(sitzung, turn)
        else:
            nummer, flaeche = self._zeigen(sitzung, turn)
        begriffe = ueberblick.get("begriffe")
        return {
            "kurs": {"course_id": kurs.course_id, "titel": kurs.titel},
            "begriffe": begriffe if isinstance(begriffe, list) else [],
            "turn": turn,
            "nummer": nummer,
            "flaeche": flaeche,
            "abgeschlossen": False,
            **liste,
        }

    # ---- Listenkurs: alle Aufgaben auf der Flaeche -------------------------
    def _liste_oeffnen(self, sitzung: _Sitzung, turn: dict[str, Any]) -> tuple[int, bool, dict[str, Any]]:
        """Listenkurs oeffnen: Bereiche melden, die erste offene Aufgabe setzen und zeigen, den Rest einreihen.

        Die Anordnung entsteht jedes Mal neu aus der Liste (deterministisch, kein eigener Zustand). Die
        Nummer einer Aufgabe ist ihre Listennummer, ihr Bereich `anordnung(gross)[nummer - 1]`. Die uebrigen
        Aufgaben (auch bestaetigte) setzt die Flaechenspur, je eine Arbeit `"<sid>:setzen:<nummer>"`.
        Liefert zusaetzlich `stand` und die Aufgaben (`[{nummer, task}]`, fuer Korrektur und Leiste der Seite).
        """
        abgaben = self._abgabestaende(sitzung.sid)
        _, eintraege = self._liste_lesen(sitzung)
        bereiche = self._bereiche(eintraege)
        erste = next((e for e in eintraege if e["status"] == "offen"), None)
        if erste is None:
            raise LearningWeg("aufgaben")  # aktiver Turn, aber keine offene Aufgabe in der Liste
        aufgabe_id, nummer = erste["task"]["id"], erste["nummer"]
        if _sperrt(turn):
            self._sperre.offen(aufgabe_id)
        else:
            self._frei(sitzung)
        rechteck = bereiche[nummer - 1]
        auftrag = self._setzauftrag(erste["task"], nummer, rechteck)  # Bilder laden, bevor die Sperre greift
        with self._flaeche_sperre:
            self._flaeche_still(sitzung, lambda s: self._penecho.bereiche_melden(s, bereiche))
            ok = self._listenaufgabe_setzen(sitzung, erste, rechteck, auftrag)
            if ok:
                self._flaeche_still(sitzung, lambda s: self._penecho.bereich_zeigen(s, rechteck))
        sitzung.aufgabe_id, sitzung.nummer, sitzung.auf_flaeche = aufgabe_id, nummer, ok
        for e in eintraege:
            if e is not erste:
                self._flaechenspur.einreihen(
                    f"{sitzung.sid}:setzen:{e['nummer']}",
                    functools.partial(self._im_hintergrund_setzen, sitzung, e, bereiche[e["nummer"] - 1],
                                      self._kartenstand_von(sitzung.sid, e["nummer"])),
                    vorrang=1,
                )
        liste = {
            "stand": self._stand_aus(sitzung.sid, eintraege, abgaben),
            "aufgaben": [{"nummer": e["nummer"], "task": e["task"]} for e in eintraege],
        }
        return nummer, ok, liste

    def _listenaufgabe_setzen(
        self, sitzung: _Sitzung, eintrag: dict[str, Any], rechteck: dict[str, int],
        auftrag: Callable[[str], None] | None = None, *, zeigen: bool = True,
    ) -> bool:
        """Eine Listenaufgabe in ihr Rechteck; hat sie eine Vorbewertung und ist offen, auch ihre Karte."""
        task, nummer = eintrag["task"], eintrag["nummer"]
        with self._flaeche_sperre:
            ok = self._flaeche_still(sitzung, auftrag or self._setzauftrag(task, nummer, rechteck, zeigen=zeigen))
            vorbewertung = eintrag.get("vorbewertung")
            if ok and eintrag["status"] == "offen" and isinstance(vorbewertung, dict):
                gelesen, urteil, text = _kartenteile(vorbewertung)
                gelandet = self._flaeche_still(sitzung, lambda s: self._penecho.rueckmeldung_zeigen(
                    s, task["id"], nummer, gelesen=gelesen, urteil=urteil, text=text, rechteck=rechteck))
                self._kartentext_merken(
                    (sitzung.sid, nummer), None if gelandet else leistentext(gelesen, urteil, text))
        return ok

    def _im_hintergrund_setzen(
        self, sitzung: _Sitzung, eintrag: dict[str, Any], rechteck: dict[str, int], kartenstand: int = 0,
    ) -> None:
        """Arbeit der Flaechenspur: Fehler nur loggen (Typ), sie treffen nie die Antwort von `oeffnen`.

        Nur fuer die zuletzt geoeffnete Sitzung: nach einem Kurswechsel (oder wenn Learning die Sitzung
        ersetzt hat) faellt die Arbeit weg. Sonst holte schon `_auf_flaeche` das alte Board zurueck oder
        legte es neu an. Geprueft unter der Flaechensperre; gesetzt wird ohne show (`zeigen=False`).
        Kam seit dem Einreihen eine frische Karte fuer diese Nummer (`kartenstand`), setzt die Arbeit nur
        die Aufgabe, nicht ihre alte Karte.
        """
        try:
            with self._flaeche_sperre:
                if not self._noch_offen(sitzung):
                    log.info("Listenaufgabe %d: Sitzung nicht mehr offen, entfaellt", eintrag["nummer"])
                    return
                if self._kartenstand_von(sitzung.sid, eintrag["nummer"]) != kartenstand:
                    eintrag = {**eintrag, "vorbewertung": None}
                if not self._listenaufgabe_setzen(sitzung, eintrag, rechteck, zeigen=False):
                    log.warning("Listenaufgabe %d nicht auf der Flaeche", eintrag["nummer"])
        except Exception as fehler:  # noqa: BLE001 - eine Aufgabe darf die Spur nie anhalten
            log.warning("Listenaufgabe %d nicht gesetzt: %s", eintrag["nummer"], _typ(fehler))

    async def _r_antwort(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._mit_body(request, lambda body: (
            self._listen_antwort(sid, body) if "nummer" in body else self._antwort(sid, body)), ablauf=True)

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
        return {"rueckmeldung": text, "flaeche": flaeche, **self._gebucht(sitzung, turn)}

    def _gebucht(self, sitzung: _Sitzung, turn: dict[str, Any]) -> dict[str, Any]:
        """Schluss nach einer gebuchten Abgabe (`antwort`, `passt`): naechste Aufgabe vormerken, Begriffe.

        Die naechste Aufgabe steht noch nicht auf der Flaeche; das macht `weiter`.
        """
        sitzung.auf_flaeche = False
        nummer = self._vormerken(sitzung, turn)
        return {"turn": turn, "nummer": nummer, "begriffe": self._begriffe(sitzung.kurs)}

    async def _r_weiter(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._ausfuehren(lambda: self._weiter(sid), ablauf=True)

    def _weiter(self, sid_roh: str) -> dict[str, Any]:
        sid, sitzung, turn = self._sitzung(sid_roh)
        if sitzung.liste:
            return self._listen_weiter(sitzung)
        if turn is None:
            turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=sid))
        nummer, flaeche = self._zeigen(sitzung, turn)
        if flaeche:
            self._board_merken(sitzung)
        return {"turn": turn, "nummer": nummer, "flaeche": flaeche}

    # ---- Flaeche zuerst: abgeben, korrektur, passt --------------------------
    # Ein Body mit `nummer` gehoert zu einem Listenkurs (Seite mit `stand.ablauf == "liste"`); sonst wie heute
    # mit `revision`. Die Abgabe im Listenkurs laeuft ohne `_ablauf`: das Bild kommt synchron unter der
    # Flaechensperre, der Modellaufruf in die Modellspur.
    async def _r_abgeben(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        try:
            body = await self._body(request)
        except _Antwort as antwort:
            return self._json(antwort.status, antwort.body)
        if "nummer" in body:
            return await self._ausfuehren(lambda: self._listen_abgeben(sid, body), ablauf=False, status=202)
        return await self._ausfuehren(lambda: self._abgeben(sid, body), ablauf=True)

    async def _r_korrektur(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._mit_body(request, lambda body: (
            self._listen_korrektur(sid, body) if "nummer" in body else self._korrektur(sid, body)), ablauf=True)

    async def _r_passt(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._mit_body(request, lambda body: (
            self._listen_passt(sid, body) if "nummer" in body else self._passt(sid, body)), ablauf=True)

    async def _r_stand(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._ausfuehren(lambda: self._stand(self._listensitzung(sid)), ablauf=False)

    async def _r_zeigen(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._mit_body(request, lambda body: self._listen_zeigen(sid, body), ablauf=False)

    def _flaechenaufgabe(
        self, sid: str, sitzung: _Sitzung, revision: int, turn: dict[str, Any] | None
    ) -> tuple[dict[str, Any], int]:
        """Die offene Aufgabe laut Learning; eine veraltete Karte fuehrt wie heute zu 409 mit Turn.

        Prueft vor Schnappschuss und Modellaufruf: Revision der Seite, Aufgabe auf der Flaeche.
        """
        if turn is None:
            turn = _turn(self._lern.rufe("learning_task_next", "learning.task.next", session_id=sid))
        task = _task(turn)
        if task is None:
            self._stand_neu(sid, sitzung, "session_state_conflict")
        if turn.get("session_revision") != revision:
            self._stand_neu(sid, sitzung, "session_revision_conflict")
        if task.get("type") not in _FLAECHEN_TYPEN:
            raise _Antwort(400, {"meldung": KEINE_FLAECHENAUFGABE})
        return task, _nummer(turn)

    def _abgeben(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        revision = _revision_aus(body)
        sid, sitzung, turn = self._sitzung(sid_roh)
        task, nummer = self._flaechenaufgabe(sid, sitzung, revision, turn)
        # Deterministische Typen: nur das Ergebnisfeld (die Handschrift); Rubrik-Typen: der ganze Bereich.
        if task.get("type") in DETERMINISTISCHE_TYPEN:
            aufnahme = self._penecho.ergebnis_schnappschuss
        else:
            aufnahme = self._penecho.schnappschuss
        try:
            png = _als_png(self._auf_flaeche(sitzung, lambda s: aufnahme(s, nummer)))
        except Exception as fehler:  # noqa: BLE001 - PenEcho weg oder Bild unlesbar
            log.warning("Schnappschuss fehlgeschlagen: %s", _typ(fehler))
            raise _Antwort(503, {"meldung": FLAECHE_WEG}) from None
        payload = {"handschrift_png_base64": base64.b64encode(png).decode("ascii")}
        return self._vorbewerten(sid, sitzung, revision, task, nummer, payload)

    def _korrektur(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        revision = _revision_aus(body)
        kennung = _vorbewertung_id_aus(body)
        answer = body.get("answer")
        if not isinstance(answer, dict):
            raise _Antwort(400, {"meldung": UNGUELTIG})
        sid, sitzung, turn = self._sitzung(sid_roh)
        task, nummer = self._flaechenaufgabe(sid, sitzung, revision, turn)
        if task.get("type") not in DETERMINISTISCHE_TYPEN:
            # Rubrik-Typen haben nichts Gelesenes (Spec): nur „Passt“ und „Überarbeiten“.
            raise _Antwort(400, {"meldung": KEINE_KORREKTUR})
        return self._vorbewerten(sid, sitzung, revision, task, nummer, {"answer": answer, "korrigiert_aus": kennung})

    def _vorbewerten(
        self, sid: str, sitzung: _Sitzung, revision: int, task: dict[str, Any], nummer: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """`learning_task_preview`, dann die Karte zur Aufgabe; die Revision bleibt unveraendert."""
        try:
            result = self._lern.rufe(
                "learning_task_preview", "learning.task.preview", session_id=sid, expected_revision=revision,
                payload=payload, schreibend=True,
            )
        except LearningAbgelehnt as fehler:
            self._vorbewertung_abgelehnt(sid, sitzung, fehler, bestaetigen=False)
        turn = _turn(result)
        vorbewertung = result.get("vorbewertung")
        if not isinstance(vorbewertung, dict) or not isinstance(vorbewertung.get("id"), str):
            raise LearningWeg("vorbewertung")
        gelesen, urteil, text = _kartenteile(vorbewertung)
        flaeche = self._flaeche_still(sitzung, lambda s: self._penecho.rueckmeldung_zeigen(
            s, task["id"], nummer, gelesen=gelesen, urteil=urteil, text=text,
        ))
        antwort = {"vorbewertung": vorbewertung, "urteil": urteil, "turn": turn, "flaeche": flaeche}
        if flaeche:
            self._board_merken(sitzung)
        else:
            # Die Karte kam nicht aufs Board (z. B. kein Platz): die Leiste zeigt die Rueckmeldung.
            antwort["karte"] = leistentext(gelesen, urteil, text)
        return antwort

    def _passt(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        revision = _revision_aus(body)
        kennung = _vorbewertung_id_aus(body)
        sid, sitzung, _ = self._sitzung(sid_roh)
        try:
            result = self._lern.rufe(
                "learning_task_confirm", "learning.task.confirm", session_id=sid, expected_revision=revision,
                payload={"vorbewertung_id": kennung}, schreibend=True,
            )
        except LearningAbgelehnt as fehler:
            self._vorbewertung_abgelehnt(sid, sitzung, fehler, bestaetigen=True)
        turn = _turn(result)
        # Gebucht wie `antwort`: KI frei, naechste Aufgabe merken; die Karte steht schon auf der Flaeche.
        self._frei(sitzung)
        if turn.get("state") == "completed":
            self._ende_setzen(sitzung.kurs, sitzung.sid)
            self._bericht(sitzung, turn)
        antwort = {"rueckmeldung": None, **self._gebucht(sitzung, turn)}
        self._board_merken(sitzung)
        return antwort

    def _vorbewertung_abgelehnt(
        self, sid: str, sitzung: _Sitzung, fehler: LearningAbgelehnt, *, bestaetigen: bool
    ) -> NoReturn:
        """Pruefungssitzung 403 (die Huelle uebt nur), ungueltig 400, sonst wie heute 409 mit Turn."""
        if fehler.code == "session_access_denied":
            raise _Antwort(403, {"code": fehler.code, "meldung": NUR_UEBEN})
        if fehler.code == "invalid_session_request":
            # Bestaetigen: nicht lesbare oder unbekannte Vorbewertung. Vorbewerten: Antwortformat falsch.
            raise _Antwort(400, {"code": fehler.code, "meldung": NICHT_BESTAETIGT if bestaetigen else NICHT_ANGENOMMEN})
        self._stand_neu(sid, sitzung, fehler.code)

    # ---- Listenkurs: Abgaben im Hintergrund, Stand je Aufgabe ----------------------------------------
    # Die Abgabestaende (`_abgaben`) leben nur im Speicher. Learning weiss, was bestaetigt ist und welche
    # Vorbewertung zuletzt gespeichert wurde; der Speicher ergaenzt nur „wird bewertet“ und „Fehler“.
    def _listensitzung(self, sid_roh: str) -> _Sitzung:
        _, sitzung, _ = self._sitzung(sid_roh)
        if not sitzung.liste:
            raise _Antwort(400, {"meldung": UNGUELTIG})
        return sitzung

    def _noch_offen(self, sitzung: _Sitzung) -> bool:
        """Ob Hintergrundarbeiten dieser Sitzung noch auf die Flaeche duerfen (zuletzt geoeffnet, nicht ersetzt)."""
        return self._zuletzt == sitzung.sid and self._sitzungen.get(sitzung.sid) is sitzung

    def _liste_lesen(self, sitzung: _Sitzung) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        result = self._lern.rufe("learning_task_list", "learning.task.list", session_id=sitzung.sid)
        return _turn(result), _listeneintraege(result)

    @staticmethod
    def _bereiche(eintraege: list[dict[str, Any]]) -> list[dict[str, int]]:
        try:
            return anordnung([_gross(e["task"]) for e in eintraege])
        except ZuVieleAufgaben:
            raise _Antwort(409, {"meldung": ZU_VIELE_AUFGABEN, "code": "zu_viele_aufgaben"}) from None

    @staticmethod
    def _listennummer(body: dict[str, Any]) -> int:
        nummer = body.get("nummer")
        if isinstance(nummer, bool) or not isinstance(nummer, int) or nummer < 1:
            raise _Antwort(400, {"meldung": UNGUELTIG})
        return nummer

    @staticmethod
    def _eintrag_zu(eintraege: list[dict[str, Any]], nummer: int) -> dict[str, Any]:
        if nummer > len(eintraege):
            raise _Antwort(400, {"meldung": UNGUELTIG})
        return eintraege[nummer - 1]

    @staticmethod
    def _revision_von(turn: dict[str, Any]) -> int:
        # Learning prueft die Revision in Listenkursen nicht, verlangt aber eine.
        revision = turn.get("session_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise LearningWeg("session_revision")
        return revision

    def _abgabestaende(self, sid: str) -> dict[int, _Abgabe]:
        """Abgaben dieser Sitzung je Nummer, als Schnappschuss.

        Vor dem Lesen bei Learning nehmen: eine Abgabe, die dazwischen fertig wird, zeigt dann hoechstens
        noch einmal „in_bewertung“ (die Seite fragt weiter), nie faelschlich „offen“.
        """
        with self._abgaben_sperre:
            return {n: a for (s, n), a in self._abgaben.items() if s == sid}

    def _kartentext_merken(self, schluessel: tuple[str, int], text: str | None) -> None:
        """Ersatztext einer Karte, die nicht auf die Flaeche kam; `None` loescht ihn (Karte gelandet, ersetzt)."""
        with self._abgaben_sperre:
            if text:
                self._kartentexte[schluessel] = text
            else:
                self._kartentexte.pop(schluessel, None)

    def _karte_unterwegs(self, sid: str, nummer: int) -> bool:
        """Wartet oder laeuft eine Flaechenarbeit, die diese Nummer samt Karte setzt? Die Spur raeumt am Ende auf."""
        return (self._flaechenspur.zustand(f"{sid}:rueckmeldung:{nummer}") is not None
                or self._flaechenspur.zustand(f"{sid}:setzen:{nummer}") is not None)

    def _kartentexte_von(self, sid: str) -> dict[int, str]:
        with self._abgaben_sperre:
            return {n: t for (s, n), t in self._kartentexte.items() if s == sid}

    def _kartenstand_von(self, sid: str, nummer: int) -> int:
        with self._abgaben_sperre:
            return self._kartenstand.get((sid, nummer), 0)

    def _laeuft(self, sid: str, nummer: int, abgabe: _Abgabe | None) -> bool:
        return (abgabe is not None and abgabe.status == "in_bewertung") or \
            self._modellspur.zustand(f"{sid}:abgabe:{nummer}") is not None

    def _stand_aus(self, sid: str, eintraege: list[dict[str, Any]], abgaben: dict[int, _Abgabe]) -> dict[str, Any]:
        """Status je Aufgabe: bestaetigt (Learning) > in_bewertung > fehler (Speicher) > rueckmeldung > offen."""
        aufgaben = []
        kartentexte = self._kartentexte_von(sid)
        for e in eintraege:
            nummer, vor, abgabe = e["nummer"], e.get("vorbewertung"), abgaben.get(e["nummer"])
            meldung = None
            if e["status"] == "bestaetigt":
                status = "bestaetigt"
            elif self._laeuft(sid, nummer, abgabe):
                status = "in_bewertung"
            elif abgabe is not None and abgabe.status == "fehler":
                status, meldung = "fehler", abgabe.meldung
            elif isinstance(vor, dict):
                status = "rueckmeldung"
            else:
                status = "offen"
            kennung = vor.get("id") if isinstance(vor, dict) else None
            aufgaben.append({
                "nummer": nummer,
                "status": status,
                "urteil": urteil_aus(vor) if isinstance(vor, dict) else None,
                "vorbewertung_id": kennung if isinstance(kennung, str) else None,
                "meldung": meldung,
            })
            if status == "rueckmeldung" and nummer in kartentexte:
                aufgaben[-1]["karte"] = kartentexte[nummer]  # Karte nicht auf der Flaeche: Text fuer die Leiste
            elif status == "rueckmeldung" and self._karte_unterwegs(sid, nummer):
                # Die Karte ist noch nicht versucht: die Seite fragt weiter, bis sie steht oder `karte` da ist.
                aufgaben[-1]["karte_ausstehend"] = True
        aktuell = next((a["nummer"] for a in aufgaben if a["status"] != "bestaetigt"), aufgaben[-1]["nummer"])
        return {"ablauf": "liste", "aktuell": aktuell, "wartend": self._modellspur.wartend(f"{sid}:abgabe:"),
                "aufgaben": aufgaben}

    def _stand(self, sitzung: _Sitzung) -> dict[str, Any]:
        abgaben = self._abgabestaende(sitzung.sid)  # vor dem Lesen bei Learning (siehe dort)
        _, eintraege = self._liste_lesen(sitzung)
        return self._stand_aus(sitzung.sid, eintraege, abgaben)

    def _listen_abgelehnt(self, sitzung: _Sitzung, fehler: LearningAbgelehnt, *, bestaetigen: bool) -> NoReturn:
        """Wie `_vorbewertung_abgelehnt`, nur mit dem Stand der Liste statt eines Turns."""
        if fehler.code == "session_access_denied":
            raise _Antwort(403, {"code": fehler.code, "meldung": NUR_UEBEN})
        if fehler.code == "invalid_session_request":
            raise _Antwort(400, {"code": fehler.code, "meldung": NICHT_BESTAETIGT if bestaetigen else NICHT_ANGENOMMEN})
        raise _Antwort(409, {"code": fehler.code, "meldung": _ablehnungstext(fehler.code),
                             "stand": self._stand(sitzung)})

    def _schon_bestaetigt(self, sitzung: _Sitzung, eintraege: list[dict[str, Any]]) -> NoReturn:
        raise _Antwort(409, {"code": "schon_bestaetigt", "meldung": SCHON_BESTAETIGT,
                             "stand": self._stand_aus(sitzung.sid, eintraege, self._abgabestaende(sitzung.sid))})

    def _listen_abgeben(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        """Bild jetzt aufnehmen, die Vorbewertung in die Modellspur; Antwort sofort (202).

        Die Nummer wird belegt, bevor das Bild kommt: eine zweite Abgabe derselben Nummer bekommt 409
        `laeuft_schon`. Scheitert etwas vor dem Einreihen, gilt wieder der vorige Stand der Nummer.
        """
        nummer = self._listennummer(body)
        sitzung = self._listensitzung(sid_roh)
        sid = sitzung.sid
        schluessel, arbeit = (sid, nummer), f"{sid}:abgabe:{nummer}"
        with self._abgaben_sperre:
            vorher = self._abgaben.get(schluessel)
            laeuft = self._laeuft(sid, nummer, vorher)
            if not laeuft:
                self._abgaben[schluessel] = _Abgabe("in_bewertung")  # eine fruehere Fehlermeldung ist damit ersetzt
                self._kartentexte.pop(schluessel, None)
        if laeuft:
            self._laeuft_schon(sitzung)
        try:
            # Schnappschuss des Speichers mit der eigenen Belegung, vor dem Lesen bei Learning (siehe dort).
            abgaben = self._abgabestaende(sid)
            turn, eintraege = self._liste_lesen(sitzung)
            eintrag = self._eintrag_zu(eintraege, nummer)
            if eintrag["status"] == "bestaetigt":
                self._schon_bestaetigt(sitzung, eintraege)
            task = eintrag["task"]
            if task.get("type") not in _FLAECHEN_TYPEN:
                raise _Antwort(400, {"meldung": KEINE_FLAECHENAUFGABE})
            rechteck = self._bereiche(eintraege)[nummer - 1]
            revision = self._revision_von(turn)
            # Wie `_abgeben`: deterministische Typen nur das Ergebnisfeld, Rubrik-Typen der ganze Bereich.
            if task.get("type") in DETERMINISTISCHE_TYPEN:
                aufnahme = self._penecho.ergebnis_schnappschuss
            else:
                aufnahme = self._penecho.schnappschuss
            try:
                png = _als_png(self._auf_flaeche(sitzung, lambda s: aufnahme(s, nummer, rechteck=rechteck)))
            except Exception as fehler:  # noqa: BLE001 - PenEcho weg oder Bild unlesbar
                log.warning("Schnappschuss fehlgeschlagen: %s", _typ(fehler))
                raise _Antwort(503, {"meldung": FLAECHE_WEG}) from None
            payload = {"handschrift_png_base64": base64.b64encode(png).decode("ascii"),
                       "attempt_id": eintrag["attempt_id"]}
            bewerten = functools.partial(self._im_hintergrund_bewerten, sitzung, eintrag, rechteck, payload, revision)
            if not self._modellspur.einreihen(arbeit, bewerten, vorrang=1):
                raise _Antwort(409, {"code": "laeuft_schon", "meldung": LAEUFT_SCHON,
                                     "stand": self._stand_aus(sid, eintraege, abgaben)})
        except BaseException:
            with self._abgaben_sperre:
                if vorher is None:
                    self._abgaben.pop(schluessel, None)
                else:
                    self._abgaben[schluessel] = vorher
            raise
        # Der Stand traegt die eigene Abgabe schon als in_bewertung: die Seite uebernimmt ihn direkt.
        stand = self._stand_aus(sid, eintraege, abgaben)
        return {"nummer": nummer, "status": "in_bewertung", "wartend": stand["wartend"], "stand": stand}

    def _laeuft_schon(self, sitzung: _Sitzung) -> NoReturn:
        """409 `laeuft_schon` mit dem Stand; ist Learning gerade weg, ohne Stand (die Seite fragt weiter)."""
        antwort: dict[str, Any] = {"code": "laeuft_schon", "meldung": LAEUFT_SCHON}
        try:
            antwort["stand"] = self._stand(sitzung)
        except LearningWeg as fehler:
            log.warning("Stand zu laeuft_schon nicht gelesen: %s", _typ(fehler))
        raise _Antwort(409, antwort)

    def _im_hintergrund_bewerten(
        self, sitzung: _Sitzung, eintrag: dict[str, Any], rechteck: dict[str, int], payload: dict[str, Any],
        revision: int,
    ) -> None:
        """Arbeit der Modellspur (ohne Sperre): `learning_task_preview`, dann die Karte in die Flaechenspur.

        Fehler werden zum Stand `fehler` mit dem Text fuer die Lernenden; geloggt nur Nummer und Typ.
        """
        nummer = eintrag["nummer"]
        schluessel = (sitzung.sid, nummer)
        try:
            result = self._lern.rufe(
                "learning_task_preview", "learning.task.preview", session_id=sitzung.sid,
                expected_revision=revision, payload=payload, schreibend=True,
            )
            vorbewertung = result.get("vorbewertung")
            if not isinstance(vorbewertung, dict) or not isinstance(vorbewertung.get("id"), str):
                raise LearningWeg("vorbewertung")
        except LearningWeg as fehler:
            meldung = BEWERTUNG_WEG if fehler.code == _BEWERTUNG_WEG_CODE else NICHT_ERREICHBAR
            self._abgabe_gescheitert(schluessel, meldung, fehler)
            return
        except LearningAbgelehnt as fehler:
            self._abgabe_gescheitert(schluessel, _ablehnungstext(fehler.code), fehler)
            return
        except Exception as fehler:  # noqa: BLE001 - die Abgabe darf nie stumm „in Bewertung“ bleiben
            self._abgabe_gescheitert(schluessel, NICHT_ERREICHBAR, fehler)
            return
        with self._abgaben_sperre:
            # Learning kennt die Vorbewertung jetzt: der Stand kommt ab hier von dort (rueckmeldung).
            self._abgaben.pop(schluessel, None)
            self._karten[schluessel] = (eintrag["task"], vorbewertung)
            self._kartenstand[schluessel] = self._kartenstand.get(schluessel, 0) + 1
        self._flaechenspur.einreihen(
            f"{sitzung.sid}:rueckmeldung:{nummer}",
            functools.partial(self._im_hintergrund_rueckmelden, sitzung, nummer, rechteck),
            vorrang=0,
        )

    def _abgabe_gescheitert(self, schluessel: tuple[str, int], meldung: str, fehler: BaseException) -> None:
        log.warning("Abgabe %d nicht bewertet: %s", schluessel[1], _typ(fehler))
        with self._abgaben_sperre:
            self._abgaben[schluessel] = _Abgabe("fehler", meldung)

    def _im_hintergrund_rueckmelden(self, sitzung: _Sitzung, nummer: int, rechteck: dict[str, int]) -> None:
        """Arbeit der Flaechenspur (vorrang 0): die neueste fertige Karte dieser Nummer neben ihre Aufgabe.

        Ohne show. Ist die Sitzung nicht mehr die offene, entfaellt die Karte; beim naechsten Oeffnen
        setzt `_liste_oeffnen` sie aus Learnings Vorbewertung.
        """
        try:
            with self._flaeche_sperre:
                with self._abgaben_sperre:
                    karte = self._karten.pop((sitzung.sid, nummer), None)
                if karte is None:
                    return
                if not self._noch_offen(sitzung):
                    log.info("Rueckmeldung %d: Sitzung nicht mehr offen, entfaellt", nummer)
                    return
                task, vorbewertung = karte
                gelesen, urteil, text = _kartenteile(vorbewertung)
                if self._flaeche_still(sitzung, lambda s: self._penecho.rueckmeldung_zeigen(
                        s, task["id"], nummer, gelesen=gelesen, urteil=urteil, text=text, rechteck=rechteck)):
                    self._kartentext_merken((sitzung.sid, nummer), None)
                    self._board_merken(sitzung)
                else:
                    log.warning("Rueckmeldung %d nicht auf der Flaeche", nummer)
                    # Die Seite zeigt sonst nirgends, was gelesen wurde: Text fuer die Leiste im Stand.
                    self._kartentext_merken((sitzung.sid, nummer), leistentext(gelesen, urteil, text))
        except Exception as fehler:  # noqa: BLE001 - eine Karte darf die Spur nie anhalten
            log.warning("Rueckmeldung %d nicht gesetzt: %s", nummer, _typ(fehler))

    def _listen_korrektur(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        """„Falsch gelesen“ im Listenkurs: wie `_korrektur` (synchron, kurz), mit `attempt_id` und Rechteck."""
        nummer = self._listennummer(body)
        kennung = _vorbewertung_id_aus(body)
        answer = body.get("answer")
        if not isinstance(answer, dict):
            raise _Antwort(400, {"meldung": UNGUELTIG})
        sitzung = self._listensitzung(sid_roh)
        sid, schluessel = sitzung.sid, (sitzung.sid, nummer)
        if self._laeuft(sid, nummer, self._abgabestaende(sid).get(nummer)):
            self._laeuft_schon(sitzung)
        turn, eintraege = self._liste_lesen(sitzung)
        eintrag = self._eintrag_zu(eintraege, nummer)
        if eintrag["status"] == "bestaetigt":
            self._schon_bestaetigt(sitzung, eintraege)
        task = eintrag["task"]
        if task.get("type") not in DETERMINISTISCHE_TYPEN:
            raise _Antwort(400, {"meldung": KEINE_KORREKTUR})
        rechteck = self._bereiche(eintraege)[nummer - 1]
        try:
            result = self._lern.rufe(
                "learning_task_preview", "learning.task.preview", session_id=sid,
                expected_revision=self._revision_von(turn), schreibend=True,
                payload={"answer": answer, "korrigiert_aus": kennung, "attempt_id": eintrag["attempt_id"]},
            )
        except LearningAbgelehnt as fehler:
            self._listen_abgelehnt(sitzung, fehler, bestaetigen=False)
        vorbewertung = result.get("vorbewertung")
        if not isinstance(vorbewertung, dict) or not isinstance(vorbewertung.get("id"), str):
            raise LearningWeg("vorbewertung")
        with self._abgaben_sperre:
            # Die Korrektur ist die neueste Karte: eine fruehere Fehlermeldung und eine noch wartende Karte
            # einer Abgabe gelten nicht mehr.
            if (alt := self._abgaben.get(schluessel)) is not None and alt.status == "fehler":
                del self._abgaben[schluessel]
            self._karten.pop(schluessel, None)
            self._kartentexte.pop(schluessel, None)
            self._kartenstand[schluessel] = self._kartenstand.get(schluessel, 0) + 1
        gelesen, urteil, text = _kartenteile(vorbewertung)
        flaeche = self._flaeche_still(sitzung, lambda s: self._penecho.rueckmeldung_zeigen(
            s, task["id"], nummer, gelesen=gelesen, urteil=urteil, text=text, rechteck=rechteck))
        antwort: dict[str, Any] = {"vorbewertung": vorbewertung, "urteil": urteil, "turn": _turn(result),
                                   "flaeche": flaeche}
        if flaeche:
            self._board_merken(sitzung)
        else:
            antwort["karte"] = leistentext(gelesen, urteil, text)
        antwort["stand"] = self._stand(sitzung)
        return antwort

    def _listen_passt(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        """Vorbewertung der Nummer buchen, die naechste offene Aufgabe in Listenfolge zeigen.

        Antwort: `stand` plus `weiter_zu` (None am Kursende), `turn` und `begriffe`. Am Kursende wie heute
        Kursende merken und Bericht (mit den Rechtecken der Liste).
        """
        nummer = self._listennummer(body)
        kennung = _vorbewertung_id_aus(body)
        sitzung = self._listensitzung(sid_roh)
        vorher, eintraege = self._liste_lesen(sitzung)
        eintrag = self._eintrag_zu(eintraege, nummer)
        bereiche = self._bereiche(eintraege)
        try:
            result = self._lern.rufe(
                "learning_task_confirm", "learning.task.confirm", session_id=sitzung.sid,
                expected_revision=self._revision_von(vorher), schreibend=True,
                payload={"vorbewertung_id": kennung, "attempt_id": eintrag["attempt_id"]},
            )
        except LearningAbgelehnt as fehler:
            self._listen_abgelehnt(sitzung, fehler, bestaetigen=True)
        return self._nach_buchung(sitzung, _turn(result), eintraege, bereiche, nummer, zeigen=True)

    def _listen_sperre(self, sitzung: _Sitzung, turn: dict[str, Any]) -> None:
        """Sperre nach Learnings aktueller Aufgabe: offen bei einer Pruefungsaufgabe, sonst `_frei`."""
        task = _task(turn)
        if task is not None and _sperrt(turn):
            self._sperre.offen(task["id"])
        else:
            self._frei(sitzung)

    @staticmethod
    def _listen_vormerken(sitzung: _Sitzung, turn: dict[str, Any], aktuell: int | None) -> None:
        """Im Listenkurs kommt die Nummer nur aus der Liste (`aktuell`), nie aus `completed + 1`."""
        task = _task(turn)
        sitzung.aufgabe_id = task["id"] if task else None
        sitzung.nummer = aktuell if task else None

    def _nach_buchung(
        self, sitzung: _Sitzung, turn: dict[str, Any], eintraege: list[dict[str, Any]],
        bereiche: list[dict[str, int]], nummer: int, *, zeigen: bool,
    ) -> dict[str, Any]:
        """Gemeinsamer Schluss von Passt und Antwort im Listenkurs.

        Sperre, Kursende samt Bericht (mit den Rechtecken der Liste), Stand und `weiter_zu`. Mit `zeigen`
        kommt die Zeile von `weiter_zu` ins Bild (Passt); nach einer Antwort bleibt die Rueckmeldung im Bild.
        """
        self._listen_sperre(sitzung, turn)
        if turn.get("state") == "completed":
            self._ende_setzen(sitzung.kurs, sitzung.sid)
            self._bericht(sitzung, turn, {e["nummer"]: bereiche[e["nummer"] - 1] for e in eintraege})
        stand = self._stand_nach_buchung(sitzung, eintraege, nummer)
        self._listen_vormerken(sitzung, turn, stand["aktuell"])
        gebucht = stand["aufgaben"][nummer - 1]["status"] == "bestaetigt"
        weiter_zu = _naechste_offene(stand["aufgaben"], nummer) if gebucht else None
        if zeigen and weiter_zu is not None:
            ziel = bereiche[weiter_zu - 1]
            self._flaeche_still(sitzung, lambda s: self._penecho.bereich_zeigen(s, ziel))
        self._board_merken(sitzung)
        return {**stand, "weiter_zu": weiter_zu, "turn": turn, "begriffe": self._begriffe(sitzung.kurs)}

    def _listen_antwort(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        """Antwort aus der Seitenleiste (Typ `code`) im Listenkurs: mit `attempt_id` buchen, Rueckmeldung ans
        Rechteck der Nummer. Kein automatisches Weiter: die Seite zeigt die Rueckmeldung, dann `weiter`.
        """
        nummer = self._listennummer(body)
        answer = body.get("answer")
        if not isinstance(answer, dict):
            raise _Antwort(400, {"meldung": UNGUELTIG})
        sitzung = self._listensitzung(sid_roh)
        sid, schluessel = sitzung.sid, (sitzung.sid, nummer)
        if self._laeuft(sid, nummer, self._abgabestaende(sid).get(nummer)):
            self._laeuft_schon(sitzung)
        vorher, eintraege = self._liste_lesen(sitzung)
        eintrag = self._eintrag_zu(eintraege, nummer)
        if eintrag["status"] == "bestaetigt":
            self._schon_bestaetigt(sitzung, eintraege)
        bereiche = self._bereiche(eintraege)
        try:
            result = self._lern.rufe(
                "learning_task_answer", "learning.task.answer", session_id=sid,
                expected_revision=self._revision_von(vorher), schreibend=True,
                payload={"answer": answer, "attempt_id": eintrag["attempt_id"]},
            )
        except LearningAbgelehnt as fehler:
            self._listen_abgelehnt(sitzung, fehler, bestaetigen=False)
        turn = _turn(result)
        with self._abgaben_sperre:
            # Die Rueckmeldung belegt das Rueckmeldungsfeld: eine wartende Karte gilt nicht mehr.
            self._abgaben.pop(schluessel, None)
            self._karten.pop(schluessel, None)
            self._kartentexte.pop(schluessel, None)
            self._kartenstand[schluessel] = self._kartenstand.get(schluessel, 0) + 1
        text = rueckmeldung_text(turn)
        flaeche = False
        if text:
            titel, rechteck = f"Rückmeldung zu Aufgabe {nummer}", bereiche[nummer - 1]
            flaeche = self._flaeche_still(sitzung, lambda s: self._penecho.rueckmeldung_setzen(
                s, eintrag["task"]["id"], nummer, titel, text, rechteck=rechteck))
        antwort = self._nach_buchung(sitzung, turn, eintraege, bereiche, nummer, zeigen=False)
        return {**antwort, "rueckmeldung": text, "flaeche": flaeche}

    def _listen_weiter(self, sitzung: _Sitzung) -> dict[str, Any]:
        """`weiter` im Listenkurs: kein Setzen am alten Raster; Stand neu lesen, `aktuell` ins Bild holen."""
        abgaben = self._abgabestaende(sitzung.sid)
        turn, eintraege = self._liste_lesen(sitzung)
        stand = self._stand_aus(sitzung.sid, eintraege, abgaben)
        self._listen_sperre(sitzung, turn)
        self._listen_vormerken(sitzung, turn, stand["aktuell"])
        bereiche = self._bereiche(eintraege)
        if turn.get("state") == "completed":
            self._ende_setzen(sitzung.kurs, sitzung.sid)
            self._bericht(sitzung, turn, {e["nummer"]: bereiche[e["nummer"] - 1] for e in eintraege})
        ziel = bereiche[stand["aktuell"] - 1]
        flaeche = self._flaeche_still(sitzung, lambda s: self._penecho.bereich_zeigen(s, ziel))
        if flaeche:
            self._board_merken(sitzung)
        return {"turn": turn, **stand, "nummer": stand["aktuell"], "flaeche": flaeche}

    def _stand_nach_buchung(self, sitzung: _Sitzung, eintraege: list[dict[str, Any]], nummer: int) -> dict[str, Any]:
        """Stand nach einer Buchung (Passt, Antwort); ist Learning gerade weg, die Liste von vorher mit der gebuchten Nummer."""
        abgaben = self._abgabestaende(sitzung.sid)
        try:
            _, neu = self._liste_lesen(sitzung)
        except LearningWeg as fehler:
            log.warning("Stand nach Passt nicht gelesen: %s", _typ(fehler))
            neu = [{**e, "status": "bestaetigt"} if e["nummer"] == nummer else e for e in eintraege]
        return self._stand_aus(sitzung.sid, neu, abgaben)

    def _listen_zeigen(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        """Die gewaehlte Aufgabe ins Bild holen (`bereich_zeigen`)."""
        nummer = self._listennummer(body)
        sitzung = self._listensitzung(sid_roh)
        _, eintraege = self._liste_lesen(sitzung)
        self._eintrag_zu(eintraege, nummer)
        rechteck = self._bereiche(eintraege)[nummer - 1]
        flaeche = self._flaeche_still(sitzung, lambda s: self._penecho.bereich_zeigen(s, rechteck))
        return {"nummer": nummer, "flaeche": flaeche}

    async def _r_zeichnung(self, request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        return await self._mit_body(request, lambda body: self._zeichnung(sid, body), ablauf=True)

    def _zeichnung(self, sid_roh: str, body: dict[str, Any]) -> dict[str, Any]:
        revision = body.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise _Antwort(400, {"meldung": UNGUELTIG})
        sid, sitzung, _ = self._sitzung(sid_roh)
        if sitzung.liste:
            # Ruling T5-c: die Seite ruft diese Route nicht (mehr); Zeichnungen gehen im Listenkurs ueber `abgeben`.
            raise _Antwort(409, {"code": "nicht_im_listenmodus", "meldung": NICHT_IM_LISTENMODUS,
                                 "stand": self._stand(sitzung)})
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
        rueckfrage = isinstance(frage, str) and bool(frage.strip())
        if frage is None or (isinstance(frage, str) and not frage.strip()):
            frage = ERKLAER_FRAGE
        if not isinstance(frage, str) or len(frage) > MAX_FRAGE:
            raise _Antwort(400, {"meldung": UNGUELTIG})
        frage = frage.strip()
        sid, sitzung, _ = self._sitzung(sid_roh)
        # "Erklär mir das" und jede neue Aufgabe beginnen ein neues Gespraech.
        if not rueckfrage or sitzung.tutor_aufgabe != sitzung.aufgabe_id:
            sitzung.tutor, sitzung.tutor_aufgabe = [], sitzung.aufgabe_id
        # Stufe 1 ohne Schnappschuss: learning_tutor_ask nimmt kein Bild an.
        result = self._lern.rufe(
            "learning_tutor_ask", "learning.tutor.ask", course_id=sitzung.kurs.course_id, session_id=sid,
            payload={"question": _mit_verlauf(sitzung.tutor, frage)},
        )
        tutor = result.get("tutor")
        if not isinstance(tutor, dict) or not isinstance(tutor.get("answer"), str):
            raise LearningWeg("tutor")
        # Nur `answer` geht an die lernende Person: `next_step` formuliert das Modell mitunter als
        # Anweisung an eine Lehrkraft, samt erwarteter Werte. Die Antwort endet ohnehin mit einer Frage.
        antwort = _ohne_quellenmarken(tutor["answer"])
        sitzung.tutor = (sitzung.tutor + [(frage, antwort)])[-TUTOR_VERLAUF:]
        return {"answer": antwort}

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

    def _aufgabenbilder(self, task: dict[str, Any]) -> list[Aufgabenbild]:
        """Bilder der Aufgabe fuer die Flaeche (Spec: „Bilder der Aufgabe als Bild“), in der Reihenfolge
        Hauptbild, linke, rechte Eintraege. Fehlt eines, entfaellt es; ist learning-api weg, kommt die
        Aufgabe ohne die restlichen Bilder.

        Das Schild eines Eintragsbilds beginnt mit seinem Etikett vom Board ("1) Graph A", "A) Graph 1"),
        aus demselben `beschriften` wie der Etikettenblock im Aufgabentext (finaler Review, I1).
        """
        presentation = task.get("presentation")
        if not isinstance(presentation, dict):
            return []
        try:
            etiketten = beschriften(str(task.get("type") or ""), presentation, [])
            etikett_von = {eid: e for e, eid in (*etiketten.links.items(), *etiketten.rechts.items())}
        except ValueError:  # mehr als 26 Eintraege: ohne Etikett
            etikett_von = {}
        eintraege: list[tuple[object, dict[str, Any]]] = [(None, presentation)]
        for seite in ("left", "right"):
            liste = presentation.get(seite)
            for e in liste if isinstance(liste, list) else []:
                if not isinstance(e, dict):
                    continue
                name = e.get("label").strip() if isinstance(e.get("label"), str) else ""
                etikett = etikett_von.get(str(e.get("id")))
                eintraege.append((f"{etikett}) {name}".strip() if etikett else name, e))
        bilder: list[Aufgabenbild] = []
        for beschriftung, eintrag in eintraege:
            name = _bildname(eintrag.get("image"))
            if name is None:
                continue
            try:
                daten = self._medium(name)
            except LearningWeg:
                log.warning("Aufgabenbilder: learning-api nicht erreichbar")
                return bilder
            if daten is None:
                log.warning("Aufgabenbild fehlt bei learning-api")
                continue
            text = beschriftung.strip() if isinstance(beschriftung, str) and beschriftung.strip() else None
            bilder.append(Aufgabenbild(text, daten, _seitenverhaeltnis(eintrag)))
        return bilder

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
