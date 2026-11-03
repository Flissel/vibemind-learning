"""Server der Lern-Huelle. Start: `python -m spaces.learning.huelle.server`."""

from __future__ import annotations

from pathlib import Path

import httpx

from .bruecke import Bruecke
from .durchreicher import Durchreicher
from .einstellung import Einstellung, einstellung_lesen
from .lern_mcp import LernMcp
from .penecho_mcp import PenEchoMcp
from .seite_app import seite_app
from .sperre import Sperre
from .weiche import Weiche


def build_app(einstellung: Einstellung) -> Weiche:
    # Eine Sperre fuer Durchreicher und Bruecke: die Bruecke stellt sie, der Durchreicher prueft sie.
    sperre = Sperre()
    durchreicher = Durchreicher(
        einstellung.penecho_url, sperre,
        frozenset({einstellung.basis, f"http://127.0.0.1:{einstellung.port}"}),
    )
    bruecke = Bruecke(
        einstellung,
        LernMcp(einstellung.mcp_url, einstellung.actor_id),
        PenEchoMcp(Path(einstellung.penecho_state)),
        sperre,
        httpx.Client(timeout=30.0, follow_redirects=False),
    )
    return Weiche(einstellung, seite_app, bruecke, durchreicher)


def uvicorn_settings(host: str, port: int) -> dict[str, object]:
    """proxy_headers bleibt aus: hinter `tailscale serve` darf X-Forwarded-* nichts umschreiben."""
    return {"host": host, "port": port, "log_level": "warning", "proxy_headers": False}


def main() -> None:
    einstellung = einstellung_lesen()
    import uvicorn

    uvicorn.run(build_app(einstellung), **uvicorn_settings(einstellung.host, einstellung.port))


if __name__ == "__main__":
    main()
