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
