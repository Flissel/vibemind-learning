"""Weichen: Zugang, Cookie, Host, Origin und Verteilung der Pfade auf Seiten- und Flaechen-Port."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.weiche import FlaechenWeiche, Weiche
from spaces.learning.huelle.zugang import COOKIE, token_erzeugen

SCHLUESSEL = "k" * 40
HOST = "lernen.example.test"
FLAECHE_HOST = "lernen.example.test:8443"
E = einstellung_lesen({
    "LEARNING_HUELLE_BASIS": f"https://{HOST}",
    "LEARNING_HUELLE_FLAECHE_BASIS": f"https://{FLAECHE_HOST}",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
    "LEARNING_HUELLE_ACTOR": "lh-1-test",
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": "11111111-1111-4111-8111-111111111111", "titel": "T"}]),
})


class Aufzeichner:
    def __init__(self, name: str, aufrufe: list[str]) -> None:
        self.name, self.aufrufe = name, aufrufe

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.aufrufe.append(self.name)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _weichen() -> tuple[Weiche, FlaechenWeiche, list[str]]:
    aufrufe: list[str] = []
    seiten = Weiche(E, Aufzeichner("seite", aufrufe), Aufzeichner("api", aufrufe))
    flaeche = FlaechenWeiche(E, Aufzeichner("durch", aufrufe))
    return seiten, flaeche, aufrufe


def _anfrage(app: Any, path: str, *, art: str = "http", host: str = HOST, cookie: str | None = None,
             methode: str = "GET", origin: str | None = None) -> list[dict[str, Any]]:
    headers = [(b"host", host.encode())]
    if cookie is not None:
        headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {"type": art, "method": methode, "path": path, "headers": headers}
    gesendet: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(nachricht: dict[str, Any]) -> None:
        gesendet.append(nachricht)

    asyncio.run(app(scope, receive, send))
    return gesendet


def _start(gesendet: list[dict[str, Any]]) -> dict[str, Any]:
    return gesendet[0]


def _gut() -> str:
    return token_erzeugen(SCHLUESSEL)


def test_zugang_antwortet_200_mit_seite_die_weiterleitet() -> None:
    seiten, _, aufrufe = _weichen()
    gesendet = _anfrage(seiten, f"/lernen/zugang/{_gut()}")
    antwort = _start(gesendet)
    assert antwort["status"] == 200
    kopf = dict(antwort["headers"])
    assert kopf[b"content-type"] == b"text/html; charset=utf-8"
    cookie = kopf[b"set-cookie"].decode()
    assert cookie.startswith(f"{COOKIE}=")
    for teil in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=2592000"):
        assert teil in cookie
    csp = kopf[b"content-security-policy"].decode()
    assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
    assert kopf[b"cache-control"] == b"no-store"
    html = gesendet[1]["body"].decode("utf-8")
    assert '<meta http-equiv="refresh" content="0;url=/lernen/">' in html
    assert '<a href="/lernen/">Weiter zum Lernen</a>' in html
    assert "<script" not in html
    assert aufrufe == []


def test_zugang_mit_ungueltigem_token_403() -> None:
    seiten, _, _ = _weichen()
    gesendet = _anfrage(seiten, "/lernen/zugang/muell")
    assert _start(gesendet)["status"] == 403
    assert gesendet[1]["body"].decode() == "Dieser Link ist abgelaufen oder ungültig."


def test_zugang_nicht_auf_dem_flaechen_port() -> None:
    _, flaeche, aufrufe = _weichen()
    gesendet = _anfrage(flaeche, f"/lernen/zugang/{_gut()}", host=FLAECHE_HOST)
    assert _start(gesendet)["status"] in (403, 404)
    assert all(k != b"set-cookie" for k, _ in _start(gesendet)["headers"])
    assert aufrufe == []


def test_ohne_cookie_403_auf_beiden_ports() -> None:
    seiten, flaeche, aufrufe = _weichen()
    for path in ("/lernen/", "/lernen/api/x"):
        assert _start(_anfrage(seiten, path))["status"] == 403
    for path in ("/", "/api/ai/command"):
        assert _start(_anfrage(flaeche, path, host=FLAECHE_HOST))["status"] == 403
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie="muell"))["status"] == 403
    assert aufrufe == []


def test_host_je_port_getrennt() -> None:
    seiten, flaeche, aufrufe = _weichen()
    cookie = _gut()
    assert _start(_anfrage(seiten, "/lernen/", host="evil.example", cookie=cookie))["status"] == 403
    assert _start(_anfrage(seiten, "/lernen/", host=FLAECHE_HOST, cookie=cookie))["status"] == 403
    assert _start(_anfrage(seiten, "/lernen/", host="127.0.0.1:8151", cookie=cookie))["status"] == 403
    assert _start(_anfrage(flaeche, "/", host=HOST, cookie=cookie))["status"] == 403
    assert _start(_anfrage(flaeche, "/", host="127.0.0.1:8150", cookie=cookie))["status"] == 403
    assert aufrufe == []


def test_seiten_port_dient_nur_lernen() -> None:
    seiten, _, aufrufe = _weichen()
    cookie = _gut()
    for host, path in ((HOST, "/lernen/api/x"), (HOST, "/lernen/"), ("127.0.0.1:8150", "/lernen"),
                       ("127.0.0.1:8150", "/lernen/api/kurse")):
        assert _start(_anfrage(seiten, path, host=host, cookie=cookie))["status"] == 200, path
    assert aufrufe == ["api", "seite", "seite", "api"]
    for path in ("/", "/app.js", "/api/ai/command", "/lernenx"):
        assert _start(_anfrage(seiten, path, cookie=cookie))["status"] == 404, path
    assert aufrufe == ["api", "seite", "seite", "api"]


def test_flaechen_port_dient_nur_penecho() -> None:
    _, flaeche, aufrufe = _weichen()
    cookie = _gut()
    for host, path in ((FLAECHE_HOST, "/"), ("127.0.0.1:8151", "/app.js"), (FLAECHE_HOST, "/lernenx")):
        assert _start(_anfrage(flaeche, path, host=host, cookie=cookie))["status"] == 200, path
    assert aufrufe == ["durch", "durch", "durch"]
    for path in ("/lernen", "/lernen/", "/lernen/api/kurse", "/lernen/huelle.js"):
        assert _start(_anfrage(flaeche, path, host=FLAECHE_HOST, cookie=cookie))["status"] == 404, path
    assert aufrufe == ["durch", "durch", "durch"]


def test_seiten_port_schreibend_nur_mit_eigener_origin() -> None:
    seiten, _, aufrufe = _weichen()
    cookie = _gut()
    for origin in (f"https://{HOST}", "http://127.0.0.1:8150", f"HTTPS://{HOST.upper()}"):
        gesendet = _anfrage(seiten, "/lernen/api/x", methode="POST", origin=origin, cookie=cookie)
        assert _start(gesendet)["status"] == 200, origin
    assert aufrufe == ["api", "api", "api"]
    for origin in (f"https://{FLAECHE_HOST}", "http://127.0.0.1:8151", "null", f"https://{HOST}/", None):
        gesendet = _anfrage(seiten, "/lernen/api/x", methode="POST", origin=origin, cookie=cookie)
        assert _start(gesendet)["status"] == 403, origin
        assert gesendet[1]["body"].decode() == "Nicht erlaubt."
    assert aufrufe == ["api", "api", "api"]
    # Lesen braucht keine Origin.
    assert _start(_anfrage(seiten, "/lernen/api/x", cookie=cookie))["status"] == 200


def test_websocket_ohne_cookie_1008() -> None:
    seiten, flaeche, aufrufe = _weichen()
    assert _anfrage(flaeche, "/ws", art="websocket", host=FLAECHE_HOST) == [{"type": "websocket.close", "code": 1008}]
    assert _anfrage(flaeche, "/ws", art="websocket", host="evil.example", cookie=_gut()) == [
        {"type": "websocket.close", "code": 1008}
    ]
    assert _anfrage(seiten, "/lernen/api/x", art="websocket", cookie=_gut()) == [
        {"type": "websocket.close", "code": 1008}
    ]
    assert aufrufe == []


def test_oeffentliche_penecho_dateien_ohne_cookie_auf_dem_flaechen_port() -> None:
    for pfad in ("/widget-renderer.js", "/ink-lab-vendor.js", "/playground/liveclay-v1.js",
                 "/visual-explainer-vendor.js"):
        for methode in ("GET", "HEAD"):
            _, flaeche, aufrufe = _weichen()
            gesendet = _anfrage(flaeche, pfad, host=FLAECHE_HOST, methode=methode)
            assert _start(gesendet)["status"] == 200, (pfad, methode)
            assert aufrufe == ["durch"]


def test_oeffentliche_penecho_datei_mit_query_string_pfad_bleibt_exakt() -> None:
    _, flaeche, aufrufe = _weichen()
    scope_path = "/widget-renderer.js"  # ASGI trennt die Query (?v=1.4.2) vom Pfad ab
    gesendet = _anfrage(flaeche, scope_path, host=FLAECHE_HOST)
    assert _start(gesendet)["status"] == 200
    assert aufrufe == ["durch"]


def test_oeffentliche_penecho_datei_nur_lesend() -> None:
    for methode in ("POST", "PUT", "DELETE", "OPTIONS"):
        _, flaeche, aufrufe = _weichen()
        gesendet = _anfrage(flaeche, "/widget-renderer.js", host=FLAECHE_HOST, methode=methode)
        assert _start(gesendet)["status"] == 403, methode
        assert aufrufe == []


def test_oeffentliche_penecho_datei_nur_exakter_pfad() -> None:
    for pfad in ("/widget-renderer.js/x", "/widget-renderer.jsx", "/widget-renderer.js/",
                 "/x/widget-renderer.js", "/playground/liveclay-v1.js/..", "/playground/"):
        _, flaeche, aufrufe = _weichen()
        gesendet = _anfrage(flaeche, pfad, host=FLAECHE_HOST)
        assert _start(gesendet)["status"] == 403, pfad
        assert aufrufe == []


def test_oeffentliche_penecho_datei_nicht_auf_dem_seiten_port() -> None:
    seiten, _, aufrufe = _weichen()
    gesendet = _anfrage(seiten, "/widget-renderer.js")
    assert _start(gesendet)["status"] == 403
    assert aufrufe == []


def test_oeffentliche_penecho_datei_fremder_host_403() -> None:
    for host in ("fremd.example.test", HOST):
        _, flaeche, aufrufe = _weichen()
        gesendet = _anfrage(flaeche, "/widget-renderer.js", host=host)
        assert _start(gesendet)["status"] == 403, host
        assert aufrufe == []
