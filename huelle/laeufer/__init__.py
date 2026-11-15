"""Code-Laeufer: fuehrt Python- und JavaScript-Code von Lernenden lokal auf dem PC aus, abgeschottet in der eigenen
WSL2-Distro `vibemind-code` (Entscheidung 15.11.2026, siehe docs/CODE_LAEUFER.md).

Der Host reicht nur einen geprueften Auftrag als JSON ueber stdin hinein; alles Weitere macht `lauf.py` in der Distro
(bubblewrap ohne Netz, Grenzen fuer Zeit, Speicher, Dateien, Prozesse). Der Quelltext steht nie auf der Kommandozeile.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Callable

from . import lauf

log = logging.getLogger(__name__)

DISTRO = "vibemind-code"
BENUTZER = "laeufer"
SKRIPT = "/usr/local/lib/vibemind/lauf.py"
STANDARD_ZEIT_S = 5
ANLAUF_S = 20          # Start der Distro und Aufraeumen, zusaetzlich zur Laufzeit
GLEICHZEITIG = 2       # mehr Laeufe zugleich kosten den PC zu viel Speicher

_plaetze = threading.BoundedSemaphore(GLEICHZEITIG)


class UngueltigerAuftrag(ValueError):
    """Sprache, Laenge oder Zeit ausserhalb des Rahmens; es wurde nichts gestartet."""


class LaeuferNichtBereit(RuntimeError):
    """Die Distro fehlt, startet nicht oder antwortet unbrauchbar (einrichten.ps1 ausfuehren)."""


@dataclass(frozen=True)
class Lauf:
    exit_code: int | None      # None bei Zeitueberschreitung
    stdout: str
    stderr: str
    zeit_ueberschritten: bool
    abgeschnitten: bool        # Ausgabe war laenger als die Grenze
    dauer_ms: int


def ausfuehren(
    sprache: str, quelltext: str, eingabe: str = "", zeit_s: int = STANDARD_ZEIT_S, *,
    starter: Callable[..., Any] = subprocess.run,
) -> Lauf:
    """Fuehrt `quelltext` einmal aus. Fehler im Code und Zeitueberschreitung sind Ergebnisse, keine Ausnahmen."""
    auftrag = {"sprache": sprache, "quelltext": quelltext, "eingabe": eingabe, "zeit_s": zeit_s}
    try:
        lauf.pruefen(auftrag)
    except ValueError as fehler:
        raise UngueltigerAuftrag(str(fehler)) from None
    befehl = ["wsl.exe", "-d", DISTRO, "-u", BENUTZER, "--cd", "/", "--", "python3", SKRIPT]
    with _plaetze:
        try:
            fertig = starter(befehl, input=json.dumps(auftrag).encode("utf-8"), capture_output=True,
                             timeout=zeit_s + ANLAUF_S, shell=False)
        except (OSError, subprocess.TimeoutExpired) as fehler:
            log.warning("Code-Laeufer nicht erreichbar: %s", type(fehler).__name__)
            raise LaeuferNichtBereit(type(fehler).__name__) from None
    if fertig.returncode != 0:
        log.warning("Code-Laeufer endete mit %s", fertig.returncode)
        raise LaeuferNichtBereit(f"exit {fertig.returncode}")
    try:
        daten = json.loads(fertig.stdout.decode("utf-8"))
        return Lauf(
            exit_code=_ganzzahl_oder_none(daten["exit"]), stdout=_text(daten["stdout"]), stderr=_text(daten["stderr"]),
            zeit_ueberschritten=daten["zeit_ueberschritten"] is True, abgeschnitten=daten["abgeschnitten"] is True,
            dauer_ms=int(daten["dauer_ms"]),
        )
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        log.warning("Code-Laeufer: Antwort unlesbar")
        raise LaeuferNichtBereit("antwort") from None


def _ganzzahl_oder_none(wert: object) -> int | None:
    if wert is None:
        return None
    if isinstance(wert, bool) or not isinstance(wert, int):
        raise TypeError("exit")
    return wert


def _text(wert: object) -> str:
    if not isinstance(wert, str):
        raise TypeError("text")
    return wert
