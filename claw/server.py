"""learning-claw MCP-Server — Host-Sidecar auf 127.0.0.1:8139.

Der Sidecar haelt den Dienstschluessel und spricht mit der Learning-Schicht
(HTTP-Adapter und Learning-MCP). Er kennt keine LearnHouse- oder PenEcho-Adresse.

API-Notiz: Host-Python traegt die 1.x-Reihe des mcp-Pakets (FastMCP, host/port
im Konstruktor), neuere Container 2.0 (MCPServer, host/port in run()). Beide
Zweige stehen hier, der passende gewinnt zur Laufzeit.
"""

import os
import sys
from pathlib import Path

# Startbar als Modul und als Datei; PKG_ROOT wie bei den Nachbar-Sidecars.
PKG_ROOT = next(p.parent for p in Path(__file__).resolve().parents if p.name == "spaces")
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from spaces.learning.claw import werkzeuge  # noqa: E402

ENV_FILE = Path(__file__).resolve().parents[1] / "deployment" / ".env"

_ENV_KEYS = (
    "LEARNING_VIBEMIND_SERVICE_KEY",
    "LEARNING_CLAW_API_URL",
    "LEARNING_CLAW_MCP_URL",
    "LEARNING_CLAW_MCP_HOST",
    "LEARNING_CLAW_MCP_PORT",
)


def _load_env_fallback() -> None:
    """Fehlende Schluessel aus der Learning-.env nachladen; nie ueberschreiben, nie loggen."""
    if not ENV_FILE.exists():
        return
    missing = [k for k in _ENV_KEYS if not os.environ.get(k)]
    if not missing:
        return
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        for k in missing:
            if line.startswith(k + "="):
                os.environ[k] = line.split("=", 1)[1].strip().strip('"').strip("'")


WERKZEUGE = (
    # Kurs aus Material (Autor-Rolle). Veroeffentlichen nur mit Token aus der Anzeige.
    werkzeuge.material_importieren,
    werkzeuge.material_status,
    werkzeuge.kurs_erzeugen,
    werkzeuge.kurs_status,
    werkzeuge.kurs_lesen,
    werkzeuge.entwurf_ablehnen,
    werkzeuge.veroeffentlichung_anzeigen,
    werkzeuge.veroeffentlichen,
    # Ueben und Tutor.
    werkzeuge.sitzung_starten,
    werkzeuge.naechste_aufgabe,
    werkzeuge.antwort_geben,
    werkzeuge.hinweis,
    werkzeuge.tutor_fragen,
    werkzeuge.fortschritt,
    # Zeichenaufgaben: oeffnen, Hinweis, Bewertung zeigen. Kein Speichern/Abgeben
    # im Namen der lernenden Person.
    werkzeuge.zeichenaufgabe_oeffnen,
    werkzeuge.zeichenhinweis,
    werkzeuge.bewertung_zeigen,
)

_load_env_fallback()
HOST = os.environ.get("LEARNING_CLAW_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("LEARNING_CLAW_MCP_PORT", "8139"))


def main() -> None:
    global HOST, PORT
    _load_env_fallback()
    HOST = os.environ.get("LEARNING_CLAW_MCP_HOST", HOST)
    PORT = int(os.environ.get("LEARNING_CLAW_MCP_PORT", str(PORT)))
    try:
        from mcp.server.mcpserver import MCPServer

        server = MCPServer("learning-claw")
        for fn in WERKZEUGE:
            server.tool()(fn)
        server.run(transport="streamable-http", host=HOST, port=PORT)
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("learning-claw", host=HOST, port=PORT)
        for fn in WERKZEUGE:
            server.tool()(fn)
        server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
