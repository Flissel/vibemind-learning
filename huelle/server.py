"""Server der Lern-Huelle. Start: `python -m spaces.learning.huelle.server`.

Ein Prozess, zwei Ports (Ruling J): der Seiten-Port liefert `/lernen/*`, der Flaechen-Port nur PenEcho.
Beide teilen eine Sperre und eine Bruecke.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from .bruecke import Bruecke
from .durchreicher import Durchreicher
from .einstellung import Einstellung, einstellung_lesen
from .lern_mcp import LernMcp
from .penecho_mcp import PenEchoMcp
from .seite_app import seite_app
from .sperre import Sperre, penecho_version_pruefen
from .weiche import FlaechenWeiche, Weiche


def build_apps(einstellung: Einstellung) -> tuple[Weiche, FlaechenWeiche]:
    """(Seiten-Weiche, Flaechen-Weiche) mit einer gemeinsamen Sperre."""
    # Eine Sperre fuer Durchreicher und Bruecke: die Bruecke stellt sie, der Durchreicher prueft sie.
    sperre = Sperre()
    # Eine ungepruefte PenEcho-Version kann neue Modellrouten haben: dann wird die Sperre nie frei.
    penecho_version_pruefen(Path(einstellung.penecho_paket), sperre)
    durchreicher = Durchreicher(
        einstellung.penecho_url, sperre, frozenset(einstellung.flaechen_herkuenfte),
        rahmen_herkuenfte=einstellung.seiten_herkuenfte,
    )
    bruecke = Bruecke(
        einstellung,
        LernMcp(einstellung.mcp_url, einstellung.actor_id),
        PenEchoMcp(Path(einstellung.penecho_state)),
        sperre,
        httpx.Client(timeout=30.0, follow_redirects=False),
        # Neben den Merkdateien von PenEchoMcp (sitzungen.json, flaechen.json).
        Path(einstellung.penecho_state).parent / "learning-huelle" / "kursende.json",
    )
    return Weiche(einstellung, seite_app(einstellung), bruecke), FlaechenWeiche(einstellung, durchreicher)


def uvicorn_settings(host: str, port: int) -> dict[str, object]:
    """proxy_headers bleibt aus: hinter `tailscale serve` darf X-Forwarded-* nichts umschreiben."""
    return {"host": host, "port": port, "log_level": "warning", "proxy_headers": False}


async def _beide(einstellung: Einstellung) -> None:
    import uvicorn

    seiten, flaeche = build_apps(einstellung)
    server = [
        uvicorn.Server(uvicorn.Config(seiten, **uvicorn_settings(einstellung.host, einstellung.port))),
        uvicorn.Server(uvicorn.Config(flaeche, **uvicorn_settings(einstellung.host, einstellung.flaeche_port))),
    ]

    async def laufen(s: object) -> None:
        try:
            await s.serve()  # type: ignore[attr-defined]
        finally:
            for anderer in server:  # endet einer, enden beide
                anderer.should_exit = True

    await asyncio.gather(*(laufen(s) for s in server))


def main() -> None:
    asyncio.run(_beide(einstellung_lesen()))


if __name__ == "__main__":
    main()
