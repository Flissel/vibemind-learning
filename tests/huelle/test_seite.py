"""Seite der Lern-Huelle: Header, CSP, Dateien ohne Adressen, Zugang nur mit Cookie."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from spaces.learning.claw.formeln import KOPF
from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.seite_app import DATEIEN, SEITE, seite_app, seiten_csp
from spaces.learning.huelle.server import build_apps
from spaces.learning.huelle.zugang import COOKIE, token_erzeugen

SCHLUESSEL = "k" * 40
HOST = "lernen.example.test"
E = einstellung_lesen({
    "LEARNING_HUELLE_BASIS": f"https://{HOST}",
    "LEARNING_HUELLE_FLAECHE_BASIS": f"https://{HOST}:8443",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
    "LEARNING_HUELLE_ACTOR": "lh-1-test",
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": "11111111-1111-4111-8111-111111111111", "titel": "T"}]),
})


def _hole(app: Any, path: str, *, methode: str = "GET", host: str = HOST, cookie: str | None = None) -> list[dict[str, Any]]:
    headers = [(b"host", host.encode())]
    if cookie is not None:
        headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
    scope = {"type": "http", "method": methode, "path": path, "headers": headers}
    gesendet: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(nachricht: dict[str, Any]) -> None:
        gesendet.append(nachricht)

    asyncio.run(app(scope, receive, send))
    return gesendet


def test_seite_hat_csp_und_keine_adressen() -> None:
    for name, (datei, art) in DATEIEN.items():
        gesendet = _hole(seite_app(E), "/lernen/" + name)
        kopf = {k.lower(): v for k, v in gesendet[0]["headers"]}
        assert gesendet[0]["status"] == 200
        assert kopf[b"content-type"].decode() == art
        csp = kopf[b"content-security-policy"].decode()
        assert "script-src 'self' https://cdn.jsdelivr.net/npm/katex@0.16.11/" in csp
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "connect-src 'self'" in csp and "img-src 'self'" in csp
        assert f"frame-src https://{HOST}:8443 http://127.0.0.1:8151" in csp and "frame-src 'self'" not in csp
        assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp
        assert "unsafe-eval" not in csp and "script-src 'unsafe-inline'" not in csp
        assert kopf[b"x-content-type-options"] == b"nosniff"
        assert kopf[b"referrer-policy"] == b"same-origin"
        assert kopf[b"cache-control"] == b"no-store"
        text = (SEITE / datei).read_text(encoding="utf-8")
        for verboten in ("100.", "ts.net", "http://", "https://", "8443", "8151"):
            assert verboten not in text, (datei, verboten)


def test_seite_traegt_katex_kopf_und_rahmen() -> None:
    html = _hole(seite_app(E), "/lernen/")[1]["body"].decode("utf-8")
    assert KOPF in html and "<!--KOPF-->" not in html
    # Die Flaeche kommt von der eigenen Flaechen-Herkunft, nicht von der Seite (Ruling J).
    assert f'<iframe id="rahmen" src="https://{HOST}:8443/?lernen=1"' in html
    lokal = _hole(seite_app(E), "/lernen/", host="127.0.0.1:8150")[1]["body"].decode("utf-8")
    assert '<iframe id="rahmen" src="http://127.0.0.1:8151/?lernen=1"' in lokal
    for text in ("Fläche groß", "Geteilt"):
        assert text in html


def test_csp_ohne_unsichere_erweiterung() -> None:
    csp = seiten_csp(E.flaechen_herkuenfte).decode()
    assert csp.count("script-src") == 1 and "'unsafe-inline'" in csp.split("style-src")[1].split(";")[0]
    assert "script-src" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_nur_bekannte_dateien_und_nur_lesen() -> None:
    app = seite_app(E)
    assert _hole(app, "/lernen/../x")[0]["status"] == 404
    assert _hole(app, "/lernen/index.html")[0]["status"] == 404
    assert _hole(app, "/lernen/", methode="POST")[0]["status"] == 405
    kopf = _hole(app, "/lernen/huelle.js", methode="HEAD")
    assert kopf[0]["status"] == 200 and kopf[1]["body"] == b""


def test_seite_nur_mit_cookie() -> None:
    weiche, _ = build_apps(E)
    assert _hole(weiche, "/lernen/")[0]["status"] == 403
    assert _hole(weiche, "/lernen/huelle.js", cookie="muell")[0]["status"] == 403
    assert _hole(weiche, "/lernen/", host="evil.example", cookie=token_erzeugen(SCHLUESSEL))[0]["status"] == 403
    mit = _hole(weiche, "/lernen/", cookie=token_erzeugen(SCHLUESSEL))
    assert mit[0]["status"] == 200 and b"<iframe" in mit[1]["body"]


def test_seitendateien_vollstaendig() -> None:
    for name in ("index.html", "huelle.css", "huelle.js", "aufgabe.js", "aufgabe.test.mjs", "flaeche.js",
                 "flaeche.test.mjs", "liste.js", "liste.test.mjs"):
        assert (Path(SEITE) / name).is_file()
    # flaecheSpeichern() und die Listenhilfen muessen fuer huelle.js ladbar sein.
    assert DATEIEN["flaeche.js"] == ("flaeche.js", "text/javascript; charset=utf-8")
    assert DATEIEN["liste.js"] == ("liste.js", "text/javascript; charset=utf-8")
    assert 'from "./liste.js"' in (Path(SEITE) / "huelle.js").read_text(encoding="utf-8")


def test_listenkopf_ist_verdrahtet() -> None:
    html = (SEITE / "index.html").read_text(encoding="utf-8")
    kopf = html.split('<header class="kopf">')[1].split("</header>")[0]
    for kennung in ("listenkopf", "aufgabe-zurueck", "aufgabe-wahl", "aufgabe-vor", "aufgabe-status", "warte-text"):
        assert f'id="{kennung}"' in kopf, kennung
    assert 'id="listenkopf" class="listenkopf" role="group" aria-label="Aufgabenwahl" hidden' in kopf
    js = (SEITE / "huelle.js").read_text(encoding="utf-8")
    for route in ("/stand", "/zeigen"):
        assert route in js, route
    # CSP: keine Inline-Styles, auch nicht in erzeugtem HTML.
    assert "style=" not in js and "style=" not in html


def test_build_apps_zwei_herkuenfte_eine_sperre() -> None:
    from spaces.learning.huelle.bruecke import Bruecke
    from spaces.learning.huelle.durchreicher import Durchreicher
    from spaces.learning.huelle.weiche import FlaechenWeiche, Weiche

    seiten, flaeche = build_apps(E)
    assert isinstance(seiten, Weiche) and isinstance(flaeche, FlaechenWeiche)
    bruecke, durchreicher = seiten._api, flaeche._durchreicher
    assert isinstance(bruecke, Bruecke) and isinstance(durchreicher, Durchreicher)
    assert bruecke._sperre is durchreicher._sperre
    assert durchreicher._eigene == frozenset({f"https://{HOST}:8443", "http://127.0.0.1:8151"})
    assert durchreicher._rahmen == f"frame-ancestors https://{HOST} http://127.0.0.1:8150"


def test_main_startet_zwei_server_in_einem_prozess(monkeypatch) -> None:
    import uvicorn

    from spaces.learning.huelle import server

    gestartet: list[tuple[int, Any]] = []

    class FakeServer:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False

        async def serve(self) -> None:
            gestartet.append((self.config.port, self.config.app))

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "einstellung_lesen", lambda: E)
    server.main()
    ports = sorted(p for p, _ in gestartet)
    assert ports == [8150, 8151]
    apps = dict(gestartet)
    from spaces.learning.huelle.weiche import FlaechenWeiche, Weiche

    assert isinstance(apps[8150], Weiche) and isinstance(apps[8151], FlaechenWeiche)
    assert apps[8150]._api._sperre is apps[8151]._durchreicher._sperre


def test_uvicorn_ohne_proxy_header() -> None:
    from spaces.learning.huelle.server import uvicorn_settings

    assert uvicorn_settings("127.0.0.1", 8151)["proxy_headers"] is False


def test_leiste_sitzt_oben_rechts_unter_der_penecho_kopfzeile() -> None:
    # Abnahme 06.11.: unten rechts verdeckte die Leiste mit Passt/Ueberarbeiten/Falsch gelesen
    # PenEchos Werkzeugleiste (Stift, Radierer) und das KI-Feld unten rechts.
    css = (SEITE / "huelle.css").read_text(encoding="utf-8")
    regel = css.split(".flaechenleiste {")[1].split("}")[0]
    assert "top:" in regel and "bottom:" not in regel
    js = (SEITE / "huelle.js").read_text(encoding="utf-8")
    assert "finden Sie oben rechts" in js and "unten rechts" not in js


def test_leiste_ueber_der_flaeche_ist_verdrahtet() -> None:
    html = (SEITE / "index.html").read_text(encoding="utf-8")
    flaeche = html.split('<section id="flaeche"')[1].split("</section>")[0]
    for kennung in ("flaechenleiste", "leisten-hinweis", "korrektur-feld", "neu-bewerten", "leisten-knoepfe"):
        assert f'id="{kennung}"' in flaeche, kennung
    css = (SEITE / "huelle.css").read_text(encoding="utf-8")
    assert ".flaechenleiste {" in css and "pointer-events: none" in css and "pointer-events: auto" in css
    mobil = css.split("@media (max-width: 699px)")[1]
    assert ".flaechenleiste" in mobil and "flex-wrap: wrap" in css
    js = (SEITE / "huelle.js").read_text(encoding="utf-8")
    assert 'from "./flaeche.js"' in js and "flaecheSpeichern(" in js
    for route in ("/abgeben", "/korrektur", "/passt"):
        assert route in js, route
    assert "alert(" not in js and "confirm(" not in js


def test_leisten_hinweis_ist_in_der_hoehe_begrenzt() -> None:
    # Abnahme 06.11. (F14): ein langer Hinweis schob "Passt" unter den Bildschirmrand.
    css = (SEITE / "huelle.css").read_text(encoding="utf-8")
    regel = css.split(".leisten-hinweis, .korrektur {")[1].split("}")[0]
    assert "max-height:" in regel and "overflow-y: auto" in regel


def test_seitenleiste_haelt_katex_hilfstext_drinnen() -> None:
    # Abnahme 06.11. (F16): KaTeX setzt .katex-mathml absolut. Ohne positionierte Seitenleiste lag der
    # Hilfstext von Formeln weit unten in der Leiste ausserhalb ihres Scrollbereichs, und die ganze Seite
    # bekam einen Scrollbalken (Kopfzeile verschoben).
    css = (SEITE / "huelle.css").read_text(encoding="utf-8")
    regel = css.split("\n.leiste {")[1].split("}")[0]
    assert "position: relative" in regel


def test_zusammenfassung_link_nur_im_listenmodus() -> None:
    html = (SEITE / "index.html").read_text(encoding="utf-8")
    js = (SEITE / "huelle.js").read_text(encoding="utf-8")
    assert 'id="zusammenfassung"' in html and "Zusammenfassung (PDF)" in html
    assert "zusammenfassung.pdf" in js and "z.liste !== null ? z.turn?.session_id" in js
