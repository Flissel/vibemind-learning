"""KI-Sperre: solange eine Pruefungsaufgabe offen (oder der Zustand unbekannt) ist, bleibt PenEcho-KI zu.

Beim Ueben bleibt die KI frei; welche Aufgabe sperrt, entscheidet die Bruecke (`bruecke._sperrt`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Fuer genau diese Version sind KI_PFADE und CLOUD_PFADE aus dem Quelltext ermittelt.
GEPRUEFTE_PENECHO_VERSION = "1.4.2"

# Befund zu PenEcho v1.4.2 (node_modules/penecho/src/server/main.js, nur gelesen).
# Modellaufruf JA (gesperrt):
#   /api/ai/command             main.js:4261   Hauptweg der KI, streamt Fortschritt
#   /api/suggest                main.js:4227   JeVision-Vorschlag ueber den Cloud-Connector (/status und
#                                              /preferences sind harmlos, fallen aber unter das Praefix)
#   /api/plugins/improve        main.js:4109   Plugin-Verbesserung per Modell
#   /api/canvas-agent/socket    canvas-agent/http.js:111  Websocket des Canvas-Agenten (Modell-Turns); die
#                                              HTTP-Routen /api/canvas-agent/projects|files|roots|... (main.js:3642)
#                                              verwalten nur Projekte und rufen kein Modell auf
#   /api/playground/liveclay    main.js:3372   Anfrage an das gehostete Live-Clay-Modell der Cloud
#   /api/settings/connections/test main.js:3879  schickt eine Testanfrage an den Modellanbieter
#   /api/community/metadata     main.js:4081   erzeugt Community-Metadaten per Modell (generateCommunityMetadata);
#                                              PenEcho fuehrt sie selbst in LOCAL_AI_ROUTES (remote-canvas-http.js:14)
# Modellaufruf NEIN (nicht gesperrt): /api/cloud/* (main.js:3493; Konto, Bibliothek, Community, Freigaben,
#   /api/cloud/models listet nur Modelle) und /api/settings/search/test (main.js:3836, Suchanbieter).
KI_PFADE: tuple[str, ...] = (
    "/api/ai/command",
    "/api/suggest",
    "/api/plugins/improve",
    "/api/canvas-agent/socket",
    "/api/playground/liveclay",
    "/api/settings/connections/test",
    "/api/community/metadata",
)

# Immer verboten, auch bei freier Sperre (main.js:3511-3528, :3498): Anmeldung, Kopplung und Freischaltung
# bei PenEcho Cloud. Ein gekoppeltes Geraet fuehrt Modell-Routen (LOCAL_AI_ROUTES) im Auftrag der Cloud direkt
# auf 127.0.0.1:3888 aus, an der Huelle vorbei; Cloud-MCP liesse ein Cloud-Modell auf der Flaeche arbeiten.
CLOUD_PFADE: tuple[str, ...] = (
    "/api/cloud/sign-in",
    "/api/cloud/pair",
    "/api/cloud/device/enable",
    "/api/cloud/mcp/access",
)

_OFFEN, _FREI, _UNBEKANNT = "offen", "frei", "unbekannt"


class Sperre:
    """Drei Zustaende; Startzustand und Fehlerfall ist `unbekannt` und zaehlt als gesperrt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._zustand = _UNBEKANNT
        self._aufgabe_id: str | None = None
        self._dauerhaft = False

    def dauerhaft_gesperrt(self) -> None:
        """Ab jetzt immer gesperrt, bis zum Neustart (etwa bei einer ungeprueften PenEcho-Version)."""
        with self._lock:
            self._dauerhaft = True

    def offen(self, aufgabe_id: str) -> None:
        with self._lock:
            self._zustand, self._aufgabe_id = _OFFEN, aufgabe_id

    def frei(self) -> None:
        with self._lock:
            self._zustand, self._aufgabe_id = _FREI, None

    def unbekannt(self) -> None:
        with self._lock:
            self._zustand, self._aufgabe_id = _UNBEKANNT, None

    def gesperrt(self) -> bool:
        with self._lock:
            return self._dauerhaft or self._zustand != _FREI


def _paket_daten(paket: Path) -> dict[str, object] | None:
    try:
        daten = json.loads((Path(paket) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return daten if isinstance(daten, dict) else None


def _kennung(daten: dict[str, object] | None) -> tuple[str, str | None]:
    """`(Name, PenEcho-Version)` einer Flaeche: PenEcho selbst oder die Lernflaeche mit ihrer Basis."""
    if daten is None:
        return "unbekannt", None
    name = daten.get("name")
    if name == "penecho":
        version = daten.get("version")
    elif name == "lernflaeche":
        fork = daten.get("lernflaeche")
        version = fork.get("basisPenecho") if isinstance(fork, dict) else None
    else:
        return (name if isinstance(name, str) else "unbekannt"), None
    return name, (version if isinstance(version, str) else None)


def penecho_version(paket: Path) -> str | None:
    """PenEcho-Version der Flaeche aus `package.json`: bei PenEcho `version`, bei der Lernflaeche
    `lernflaeche.basisPenecho`; None, wenn nicht lesbar oder keine bekannte Flaeche."""
    return _kennung(_paket_daten(paket))[1]


def penecho_version_pruefen(paket: Path, sperre: Sperre) -> bool:
    """Nur die gepruefte Version darf die Sperre je loesen; sonst bleibt die KI bis zum Neustart zu.

    Die KI-Pfade oben stammen aus dem Quelltext genau dieser Version. Eine neuere PenEcho-Version
    kann neue Modellrouten haben, die die Sperre nicht kennt.
    """
    name, version = _kennung(_paket_daten(paket))
    if version == GEPRUEFTE_PENECHO_VERSION:
        return True
    sperre.dauerhaft_gesperrt()

    def kurz(text: str | None) -> str:
        return re.sub(r"[^0-9A-Za-z.+-]", "", text)[:40] if text else "unbekannt"

    log.warning("Flaeche %s mit PenEcho-Version %s ist nicht geprueft (erwartet %s); die KI bleibt gesperrt",
                kurz(name), kurz(version), GEPRUEFTE_PENECHO_VERSION)
    return False
