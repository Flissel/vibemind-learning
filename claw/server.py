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
from typing import Any

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

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8139
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def resolve_host_port() -> tuple[str, int]:
    """Host und Port aus der Umgebung; nur loopback ist erlaubt.

    Der Sidecar haelt den Schluessel mit Veroeffentlichungs-Rolle und darf nie
    im Netz lauschen.
    """
    host = os.environ.get("LEARNING_CLAW_MCP_HOST", DEFAULT_HOST).strip()
    port = int(os.environ.get("LEARNING_CLAW_MCP_PORT", str(DEFAULT_PORT)))
    if host.lower() not in _LOOPBACK_HOSTS:
        raise SystemExit(f"learning-claw bindet nur loopback, nicht {host!r}")
    return host, port


def transport_security_settings(port: int) -> Any:
    """DNS-Rebinding-Schutz des mcp-Pakets (1.x), auf loopback eingeschraenkt."""
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts],
    )


def main() -> None:
    _load_env_fallback()
    host, port = resolve_host_port()
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        MCPServer = None  # noqa: N806
    if MCPServer is not None:
        server = MCPServer("learning-claw")
        for fn in WERKZEUGE:
            server.tool()(fn)
        server.run(transport="streamable-http", host=host, port=port)
        return
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "learning-claw",
        host=host,
        port=port,
        transport_security=transport_security_settings(port),
    )
    for fn in WERKZEUGE:
        server.tool()(fn)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
