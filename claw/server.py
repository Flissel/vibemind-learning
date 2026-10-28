"""learning-claw MCP-Server — Host-Sidecar auf 127.0.0.1:8139.

Der Sidecar haelt den Dienstschluessel und spricht mit der Learning-Schicht
(HTTP-Adapter und Learning-MCP). Er kennt keine LearnHouse- oder PenEcho-Adresse.

API-Notiz: Host-Python traegt die 1.x-Reihe des mcp-Pakets (FastMCP, host/port
im Konstruktor), neuere Container 2.0 (MCPServer, host/port in run()). Beide
Zweige stehen hier, der passende gewinnt zur Laufzeit.
"""

import hmac
import os
import sys
from pathlib import Path
from typing import Any

# Startbar als Modul und als Datei; PKG_ROOT wie bei den Nachbar-Sidecars.
PKG_ROOT = next(p.parent for p in Path(__file__).resolve().parents if p.name == "spaces")
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from spaces.learning.claw import werkzeuge  # noqa: E402
from spaces.learning.claw.freigabe_seite import PREFIX as FREIGABE_PREFIX  # noqa: E402
from spaces.learning.claw.freigabe_seite import FreigabeSeite  # noqa: E402
from spaces.learning.claw.pruefung_seite import PREFIX as PRUEFUNG_PREFIX  # noqa: E402
from spaces.learning.claw.pruefung_seite import PruefungSeite  # noqa: E402
from spaces.learning.claw.zeichen_seite import PREFIX as ZEICHEN_PREFIX  # noqa: E402
from spaces.learning.claw.zeichen_seite import ZeichenSeite  # noqa: E402

ENV_FILE = Path(__file__).resolve().parents[1] / "deployment" / ".env"

_ENV_KEYS = (
    "LEARNING_VIBEMIND_SERVICE_KEY",
    "LEARNING_CLAW_API_URL",
    "LEARNING_CLAW_MCP_URL",
    "LEARNING_CLAW_MCP_HOST",
    "LEARNING_CLAW_MCP_PORT",
)

# Das Token kommt nur aus der Prozessumgebung (Startskript), nie aus einer Datei.
TOKEN_ENV = "LEARNING_CLAW_MCP_TOKEN"
MIN_TOKEN_LENGTH = 32


def _load_env_fallback() -> None:
    """Fehlende Schluessel aus der Learning-.env nachladen; nie ueberschreiben, nie loggen."""
    env_file = Path(os.environ.get("LEARNING_CLAW_ENV_FILE") or ENV_FILE)
    if not env_file.exists():
        return
    missing = [k for k in _ENV_KEYS if not os.environ.get(k)]
    if not missing:
        return
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        for k in missing:
            if line.startswith(k + "="):
                os.environ[k] = line.split("=", 1)[1].strip().strip('"').strip("'")


WERKZEUGE = (
    # Kurs aus Material (Autor-Rolle). Veroeffentlichen kann nur die Person selbst:
    # auf der lokalen Freigabe-Seite (freigabe_seite.py), nicht das Modell.
    werkzeuge.material_importieren,
    werkzeuge.material_status,
    werkzeuge.kurs_erzeugen,
    werkzeuge.kurs_status,
    werkzeuge.kurs_lesen,
    werkzeuge.entwurf_ablehnen,
    werkzeuge.veroeffentlichung_anzeigen,
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
    # Zurueckgehaltene KI-Bewertungen: anzeigen; entscheiden nur die Person auf der
    # lokalen Pruefseite (pruefung_seite.py).
    werkzeuge.pruefungen_anzeigen,
    # Nightly source check: which courses should be looked at again.
    werkzeuge.quellen_frische,
    # Werkstatt: Kurse und Aufgaben als nummerierte Fassungen. Nichts davon veroeffentlicht;
    # fuer Lernende wird es erst nach der Freigabe durch die Inhaberin live.
    werkzeuge.werkstatt_kurs_neu,
    werkzeuge.werkstatt_kurs_zeigen,
    werkzeuge.werkstatt_kurs_aendern,
    werkzeuge.werkstatt_kurs_uebernehmen,
    werkzeuge.werkstatt_grobentwurf_neu,
    werkzeuge.werkstatt_aufgabe_neu,
    werkzeuge.werkstatt_aufgabe_ausformulieren,
    werkzeuge.werkstatt_aufgabe_aendern,
    werkzeuge.werkstatt_aufgabe_loeschen,
    werkzeuge.werkstatt_fassungen,
    werkzeuge.werkstatt_zurueckholen,
    werkzeuge.werkstatt_vorschau,
    werkzeuge.werkstatt_auftrag,
)

SESSION_HEADER = "mcp-session-id"
def register_tools(server: Any) -> None:
    for fn in WERKZEUGE:
        server.tool()(fn)


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


def require_token() -> str:
    """Gemeinsames Bearer-Geheimnis; ohne ausreichend langes Token startet der Sidecar nicht.

    Der Container erreicht jeden Loopback-Port des Hosts ueber host.docker.internal.
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"{TOKEN_ENV} fehlt oder ist kuerzer als {MIN_TOKEN_LENGTH} Zeichen")
    return token


SERVICE_KEY_ENV = "LEARNING_VIBEMIND_SERVICE_KEY"


def require_service_key() -> str:
    """Dienstschluessel fuer die Learning-Schicht; ohne ausreichend langen startet der Sidecar nicht.

    Gelesen nach dem .env-Rueckfall; der Wert erscheint in keiner Meldung.
    """
    key = os.environ.get(SERVICE_KEY_ENV, "").strip()
    if len(key) < MIN_TOKEN_LENGTH:
        raise SystemExit(
            f"{SERVICE_KEY_ENV} fehlt oder ist kuerzer als {MIN_TOKEN_LENGTH} Zeichen "
            "(Prozessumgebung oder deployment/.env)"
        )
    return key


class BearerAuth:
    """ASGI-Huelle: jede HTTP-Anfrage braucht `Authorization: Bearer <token>`.

    Vergleich in konstanter Zeit; die 401-Antwort gibt nichts zurueck.
    """

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and not self._authorized(scope):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._app(scope, receive, send)

    def _authorized(self, scope: Any) -> bool:
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                prefix = b"Bearer "
                if not value.startswith(prefix):
                    return False
                return hmac.compare_digest(value[len(prefix):], self._token)
        return False


class FreigabeWeiche:
    """`/freigabe/...`, `/zeichnen/...` und `/pruefung/...` an die lokalen Seiten, alles
    andere hinter BearerAuth."""

    def __init__(self, mcp_app: Any, seite: Any, zeichnen: Any = None, pruefung: Any = None) -> None:
        self._mcp_app = mcp_app
        self._seite = seite
        self._zeichnen = zeichnen
        self._pruefung = pruefung

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path.startswith(FREIGABE_PREFIX):
            await self._seite(scope, receive, send)
            return
        if self._zeichnen is not None and path.startswith(ZEICHEN_PREFIX):
            await self._zeichnen(scope, receive, send)
            return
        if self._pruefung is not None and path.startswith(PRUEFUNG_PREFIX):
            await self._pruefung(scope, receive, send)
            return
        await self._mcp_app(scope, receive, send)


def build_app(server: Any, token: str, port: int = DEFAULT_PORT) -> Any:
    """Streamable-HTTP-App des MCP-Servers hinter BearerAuth, plus die Freigabe-Seite."""
    factory = getattr(server, "streamable_http_app", None)
    if factory is None:
        raise SystemExit("MCP-Paket bietet keine ASGI-App; ohne Token-Pruefung wird nicht gestartet")
    return FreigabeWeiche(
        BearerAuth(factory(), token), FreigabeSeite(port), ZeichenSeite(port), PruefungSeite(port)
    )


def main() -> None:
    _load_env_fallback()
    host, port = resolve_host_port()
    token = require_token()
    require_service_key()
    import uvicorn

    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        MCPServer = None  # noqa: N806
    if MCPServer is not None:
        server = MCPServer("learning-claw")
    else:
        from mcp.server.fastmcp import FastMCP

        server = FastMCP(
            "learning-claw",
            host=host,
            port=port,
            transport_security=transport_security_settings(port),
        )
    register_tools(server)
    uvicorn.run(build_app(server, token, port), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
