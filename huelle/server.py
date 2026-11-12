"""Server der Lern-Huelle. Start: `python -m spaces.learning.huelle.server`.

Ein Prozess, zwei Ports (Ruling J): der Seiten-Port liefert `/lernen/*`, der Flaechen-Port nur PenEcho.
Beide teilen eine Sperre und eine Bruecke.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from .bruecke import Bruecke
from .durchreicher import Durchreicher
from .einstellung import Einstellung, einstellung_lesen
from .gruppe import AbgleichDienst, Gruppen
from .katalog import TIMEOUT_S, Katalog
from .lern_mcp import LernMcp
from .lernflaeche_bewertung import KiBewerter
from .penecho_mcp import PenEchoMcp
from .seite_app import seite_app
from .sperre import Sperre, penecho_version_pruefen
from .weiche import FlaechenWeiche, Weiche


# Client-Bibliotheken loggen Adressen: websockets den Handshake mit Ausweis (DEBUG), httpx/httpcore URLs mit der
# Kennung (INFO/DEBUG). Sie bleiben auf WARNING, egal wie der Rest eingestellt ist.
BIBLIOTHEKS_LOGGER = ("httpx", "httpcore", "websockets")


def bibliotheken_leise() -> None:
    for name in BIBLIOTHEKS_LOGGER:
        logging.getLogger(name).setLevel(logging.WARNING)


def build_apps(einstellung: Einstellung) -> tuple[Weiche, FlaechenWeiche]:
    """(Seiten-Weiche, Flaechen-Weiche) mit einer gemeinsamen Sperre."""
    seiten, flaeche, _ = _bauen(einstellung)
    return seiten, flaeche


def _bauen(einstellung: Einstellung) -> tuple[Weiche, FlaechenWeiche, Bruecke]:
    """Wie `build_apps`, dazu die Bruecke, deren Spuren `_beide` beim Herunterfahren beendet."""
    bibliotheken_leise()
    # Eine Sperre fuer Durchreicher und Bruecke: die Bruecke stellt sie, der Durchreicher prueft sie.
    sperre = Sperre()
    # Eine ungepruefte PenEcho-Version kann neue Modellrouten haben: dann wird die Sperre nie frei.
    penecho_version_pruefen(Path(einstellung.penecho_paket), sperre)
    # Ein Katalog (ein Puffer) fuer Bruecke und Flaechen-Port: beide pruefen Personen gegen dieselben Kurse.
    katalog = Katalog(einstellung, httpx.Client(timeout=TIMEOUT_S, follow_redirects=False))
    # Gruppenflaechen (Teil C) nur mit vollstaendiger Einstellung; sonst bleibt /api/abgleich zu (1008).
    gruppen = (Gruppen(einstellung.learnhouse_url, einstellung.dienst_schluessel,
                       httpx.Client(timeout=TIMEOUT_S, follow_redirects=False))
               if einstellung.gruppen_aktiv else None)
    # Platz der Mappe und Schutzfelder beim Abgleichdienst (Teil C); nur zusammen mit `gruppen`.
    abgleich = (AbgleichDienst(einstellung.abgleich_url, einstellung.abgleich_schluessel,
                               httpx.Client(timeout=TIMEOUT_S, follow_redirects=False))
                if gruppen is not None else None)
    bruecke = Bruecke(
        einstellung,
        # Ohne Vorgabe-Kennung: jeder Aufruf nennt die Kennung der Anfrage (request.state.actor_id).
        LernMcp(einstellung.mcp_url),
        PenEchoMcp(Path(einstellung.penecho_state), url=einstellung.penecho_mcp_url),
        sperre,
        httpx.Client(timeout=30.0, follow_redirects=False),
        # Neben den Merkdateien von PenEchoMcp (sitzungen.json, flaechen.json).
        Path(einstellung.penecho_state).parent / "learning-huelle" / "kursende.json",
        katalog=katalog,
        gruppen=gruppen,
        abgleich=abgleich,
        bewerter=KiBewerter(einstellung.lernflaeche_url, einstellung.bewertung_schluessel),
    )
    durchreicher = Durchreicher(
        einstellung.penecho_url, sperre, frozenset(einstellung.flaechen_herkuenfte),
        rahmen_herkuenfte=einstellung.seiten_herkuenfte,
        # PenEchos Server-Ablage: jede Person nur die Boards, die die Bruecke ihren Sitzungen zugeordnet hat.
        ablage=bruecke.eigene_dokumente,
        # Jede andere Kennung (Personen-Cookie) erreicht nur die Lernmodus-Routen (Allowlist).
        inhaber=einstellung.actor_id,
        # Abgleich der Gruppenflaeche: Mitgliedschaft pruefen, Person und Ausweis selbst setzen.
        gruppen=gruppen,
        abgleich_schluessel=einstellung.abgleich_schluessel if gruppen is not None else "",
        # Freie KI der Personen: je Person eine, gesamt begrenzt (der Inhaber nicht).
        ki_gleichzeitig=einstellung.ki_gleichzeitig,
    )
    flaeche = FlaechenWeiche(einstellung, durchreicher, katalog)
    return Weiche(einstellung, seite_app(einstellung), bruecke), flaeche, bruecke


def uvicorn_settings(host: str, port: int) -> dict[str, object]:
    """proxy_headers bleibt aus: hinter `tailscale serve` darf X-Forwarded-* nichts umschreiben."""
    return {"host": host, "port": port, "log_level": "warning", "proxy_headers": False}


async def _beide(einstellung: Einstellung) -> None:
    import uvicorn

    seiten, flaeche, bruecke = _bauen(einstellung)
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

    try:
        await asyncio.gather(*(laufen(s) for s in server))
    finally:
        # Hintergrund-Spuren der Bruecke anhalten: Wartendes verfaellt, Laufendes endet noch.
        await asyncio.to_thread(bruecke.beenden)


def main() -> None:
    asyncio.run(_beide(einstellung_lesen()))


if __name__ == "__main__":
    main()
