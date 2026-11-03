"""Server der Lern-Huelle. Start: `python -m spaces.learning.huelle.server`."""

from __future__ import annotations

from typing import Any

from .einstellung import Einstellung, einstellung_lesen
from .weiche import Weiche


def _platzhalter(text: str) -> Any:
    """Platzhalter, bis die Folge-Tasks (Seite, API, Durchreicher) die echten Apps liefern."""

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})
            return
        body = text.encode("utf-8")
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})

    return app


def build_app(einstellung: Einstellung) -> Weiche:
    # Task 5/7/8: durchreicher_app, api_app und seite_app ersetzen die Platzhalter.
    return Weiche(
        einstellung,
        _platzhalter("Die Seite ist noch nicht verfügbar."),
        _platzhalter("Die Schnittstelle ist noch nicht verfügbar."),
        _platzhalter("Die Fläche ist gerade nicht erreichbar."),
    )


def uvicorn_settings(host: str, port: int) -> dict[str, object]:
    """proxy_headers bleibt aus: hinter `tailscale serve` darf X-Forwarded-* nichts umschreiben."""
    return {"host": host, "port": port, "log_level": "warning", "proxy_headers": False}


def main() -> None:
    einstellung = einstellung_lesen()
    import uvicorn

    uvicorn.run(build_app(einstellung), **uvicorn_settings(einstellung.host, einstellung.port))


if __name__ == "__main__":
    main()
