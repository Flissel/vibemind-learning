"""KI-Sperre: solange eine Aufgabe offen (oder der Zustand unbekannt) ist, bleibt PenEcho-KI zu."""

from __future__ import annotations

import threading

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
# Modellaufruf NEIN (nicht gesperrt): /api/cloud/* (main.js:3493; Konto, Bibliothek, Community, Freigaben,
#   /api/cloud/models listet nur Modelle) und /api/settings/search/test (main.js:3836, Suchanbieter).
KI_PFADE: tuple[str, ...] = (
    "/api/ai/command",
    "/api/suggest",
    "/api/plugins/improve",
    "/api/canvas-agent/socket",
    "/api/playground/liveclay",
    "/api/settings/connections/test",
)

_OFFEN, _FREI, _UNBEKANNT = "offen", "frei", "unbekannt"


class Sperre:
    """Drei Zustaende; Startzustand und Fehlerfall ist `unbekannt` und zaehlt als gesperrt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._zustand = _UNBEKANNT
        self._aufgabe_id: str | None = None

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
            return self._zustand != _FREI
