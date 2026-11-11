"""Weichen: Zugang, Cookie, Host, Origin und Verteilung der Pfade auf Seiten- und Flaechen-Port."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from spaces.learning.huelle.einstellung import Kurs, einstellung_lesen
from spaces.learning.huelle.gruppe import GRUPPE_WEG
from spaces.learning.huelle.katalog import KEIN_ZUGANG_MEHR, KeinZugang, UebersichtWeg
from spaces.learning.huelle.person import PERSON_COOKIE, person_aus_cookie, person_cookie
from spaces.learning.huelle.weiche import ABGELAUFEN, FlaechenWeiche, Weiche
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
        self.personen: list[object] = []  # scope["state"]["actor_id"] je Aufruf

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.aufrufe.append(self.name)
        self.personen.append(scope.get("state", {}).get("actor_id"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _weichen() -> tuple[Weiche, FlaechenWeiche, list[str]]:
    aufrufe: list[str] = []
    seiten = Weiche(E, Aufzeichner("seite", aufrufe), Aufzeichner("api", aufrufe))
    flaeche = FlaechenWeiche(E, Aufzeichner("durch", aufrufe))
    return seiten, flaeche, aufrufe


def _anfrage(app: Any, path: str, *, art: str = "http", host: str = HOST, cookie: str | None = None,
             methode: str = "GET", origin: str | None = None,
             raw_path: bytes | None = None, cookie_kopf: str | None = None,
             query: bytes = b"") -> list[dict[str, Any]]:
    headers = [(b"host", host.encode())]
    if cookie is not None:
        headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
    if cookie_kopf is not None:
        headers.append((b"cookie", cookie_kopf.encode("utf-8")))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {"type": art, "method": methode, "path": path, "headers": headers, "query_string": query}
    if raw_path is not None:
        scope["raw_path"] = raw_path
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


def test_oeffentliche_penecho_datei_roher_pfad_mit_prozent_bekommt_keine_ausnahme() -> None:
    # Parser-Differenz: dekodierter Pfad passt, der rohe (weitergereichte) nicht.
    for roh in (b"/widget-renderer%2Ejs", b"/widget-renderer.js%00", b"/%77idget-renderer.js"):
        _, flaeche, aufrufe = _weichen()
        gesendet = _anfrage(flaeche, "/widget-renderer.js", host=FLAECHE_HOST, raw_path=roh)
        assert _start(gesendet)["status"] == 403, roh
        assert aufrufe == []


def test_oeffentliche_penecho_datei_roher_pfad_exakt_geht_durch() -> None:
    for roh in (b"/widget-renderer.js", b"/widget-renderer.js?v=1.4.2"):
        _, flaeche, aufrufe = _weichen()
        gesendet = _anfrage(flaeche, "/widget-renderer.js", host=FLAECHE_HOST, raw_path=roh)
        assert _start(gesendet)["status"] == 200, roh
        assert aufrufe == ["durch"]


# ---- Uebergabe aus LearnHouse und Personen-Cookie (Teil B) -----------------------------------------

E_LH = einstellung_lesen({
    "LEARNING_HUELLE_BASIS": f"https://{HOST}",
    "LEARNING_HUELLE_FLAECHE_BASIS": f"https://{FLAECHE_HOST}",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
    "LEARNING_HUELLE_ACTOR": "lh-1-test",
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": "11111111-1111-4111-8111-111111111111", "titel": "T"}]),
    "LEARNING_HUELLE_LEARNHOUSE_URL": "http://learnhouse.test",
})
KURS_B = "55555555-5555-4555-8555-555555555555"


class FakeKatalog:
    """Katalog.kurse je Kennung: `entfernt` -> KeinZugang, `weg` -> UebersichtWeg, sonst ein Kurs."""

    def __init__(self) -> None:
        self.entfernt: set[str] = set()
        self.weg = False
        self.fragen: list[str] = []

    def kurse(self, actor_id: str) -> tuple[Kurs, ...]:
        self.fragen.append(actor_id)
        if actor_id in self.entfernt:
            raise KeinZugang()
        if self.weg:
            raise UebersichtWeg()
        return (Kurs(KURS_B, "Kurs B"),)


def _weichen_lh(einstellung: Any = E_LH, katalog: Any = None,
                ) -> tuple[Weiche, FlaechenWeiche, Aufzeichner, Aufzeichner, Aufzeichner]:
    aufrufe: list[str] = []
    seite, api, durch = Aufzeichner("seite", aufrufe), Aufzeichner("api", aufrufe), Aufzeichner("durch", aufrufe)
    katalog = katalog or FakeKatalog()
    return Weiche(einstellung, seite, api), FlaechenWeiche(einstellung, durch, katalog), seite, api, durch


def _uebergabe_token(actor: str, kurs: str | None, *, ablauf: int | None = None) -> str:
    import base64
    import hashlib
    import hmac
    import time

    ablauf = int(time.time()) + 120 if ablauf is None else ablauf
    payload = base64.urlsafe_b64encode(f"{actor}|{kurs or ''}|{ablauf}".encode()).decode().rstrip("=")
    key = hashlib.sha256(b"uebergabe:" + SCHLUESSEL.encode()).digest()
    return f"{payload}.{hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:48]}"


def _person(actor: str) -> str:
    return f"{PERSON_COOKIE}={person_cookie(SCHLUESSEL, actor)}"


def test_uebergabe_setzt_personen_cookie_und_leitet_weiter() -> None:
    seiten, _, seite, api, _ = _weichen_lh()
    gesendet = _anfrage(seiten, f"/lernen/uebergabe/{_uebergabe_token('lh-1-person-a', KURS_B)}")
    antwort = _start(gesendet)
    assert antwort["status"] == 200
    kopf = dict(antwort["headers"])
    assert kopf[b"content-type"] == b"text/html; charset=utf-8"
    cookie = kopf[b"set-cookie"].decode()
    name, _, rest = cookie.partition("=")
    assert name == PERSON_COOKIE
    wert = rest.split(";", 1)[0]
    assert person_aus_cookie(SCHLUESSEL, wert) == "lh-1-person-a"
    for teil in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=43200"):
        assert teil in cookie
    csp = kopf[b"content-security-policy"].decode()
    assert "default-src 'none'" in csp and "script-src" not in csp
    assert kopf[b"cache-control"] == b"no-store" and kopf[b"referrer-policy"] == b"no-referrer"
    html = gesendet[1]["body"].decode("utf-8")
    assert f'<meta http-equiv="refresh" content="0;url=/lernen/?kurs={KURS_B}">' in html
    assert f'<a href="/lernen/?kurs={KURS_B}">Weiter zum Lernen</a>' in html
    assert "<script" not in html
    assert seite.aufrufe == []
    # Ohne Kurs: auf die Seite ohne Vorwahl.
    html = _anfrage(seiten, f"/lernen/uebergabe/{_uebergabe_token('lh-1-person-a', None)}")[1]["body"].decode()
    assert 'content="0;url=/lernen/"' in html


def _set_cookies(antwort: dict[str, Any]) -> dict[str, str]:
    """Alle Set-Cookie-Kopfzeilen der Antwort, nach Cookie-Name."""
    aus: dict[str, str] = {}
    for name, wert in antwort["headers"]:
        if name == b"set-cookie":
            text = wert.decode()
            aus[text.split("=", 1)[0]] = text
    return aus


def _geloescht(cookie: str) -> None:
    teile = [t.strip() for t in cookie.split(";")]
    assert teile[0].endswith("=") and teile[0].count("=") == 1, cookie  # leerer Wert
    for teil in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=0"):
        assert teil in teile, (teil, cookie)


def test_uebergabe_loescht_das_zugangscookie() -> None:
    # Geteiltes Geraet: wer per Uebergabe kommt, arbeitet nie still mit dem Notweg-Cookie weiter.
    seiten, _, _, _, _ = _weichen_lh()
    antwort = _start(_anfrage(seiten, f"/lernen/uebergabe/{_uebergabe_token('lh-1-person-a', KURS_B)}"))
    cookies = _set_cookies(antwort)
    assert set(cookies) == {PERSON_COOKIE, COOKIE}
    _geloescht(cookies[COOKIE])
    assert "Max-Age=43200" in cookies[PERSON_COOKIE]


def test_zugang_loescht_das_personen_cookie() -> None:
    # Und umgekehrt: der Notweg-Link beendet eine gemerkte Person auf diesem Geraet.
    for einstellung in (E, E_LH):
        seiten, _, _, _, _ = _weichen_lh(einstellung)
        antwort = _start(_anfrage(seiten, f"/lernen/zugang/{_gut()}"))
        cookies = _set_cookies(antwort)
        assert set(cookies) == {COOKIE, PERSON_COOKIE}
        _geloescht(cookies[PERSON_COOKIE])
        assert "Max-Age=2592000" in cookies[COOKIE]


def test_ungueltige_links_loeschen_nichts() -> None:
    seiten, _, _, _, _ = _weichen_lh()
    for pfad in ("/lernen/zugang/muell", "/lernen/uebergabe/muell"):
        assert _set_cookies(_start(_anfrage(seiten, pfad))) == {}


def test_uebergabe_ungueltig_oder_abgelaufen_403() -> None:
    import time

    seiten, _, _, _, _ = _weichen_lh()
    for token in ("muell", "é", _uebergabe_token("lh-1-person-a", KURS_B, ablauf=int(time.time()) - 1),
                  person_cookie(SCHLUESSEL, "lh-1-person-a"), _gut()):
        gesendet = _anfrage(seiten, f"/lernen/uebergabe/{token}")
        assert _start(gesendet)["status"] == 403, token
        assert all(k != b"set-cookie" for k, _ in _start(gesendet)["headers"])
        assert gesendet[1]["body"].decode() == ABGELAUFEN


def test_uebergabe_nicht_auf_dem_flaechen_port() -> None:
    _, flaeche, _, _, durch = _weichen_lh()
    gesendet = _anfrage(flaeche, f"/lernen/uebergabe/{_uebergabe_token('lh-1-person-a', KURS_B)}", host=FLAECHE_HOST)
    assert _start(gesendet)["status"] in (403, 404)
    assert all(k != b"set-cookie" for k, _ in _start(gesendet)["headers"])
    assert durch.aufrufe == []


def test_actor_je_anfrage_aus_dem_cookie() -> None:
    seiten, flaeche, seite, api, durch = _weichen_lh()
    a, b = _person("lh-1-person-a"), _person("lh-1-person-b")
    assert _start(_anfrage(seiten, "/lernen/api/kurse", cookie_kopf=a))["status"] == 200
    assert _start(_anfrage(seiten, "/lernen/", cookie_kopf=b))["status"] == 200
    # Das Personen-Cookie gewinnt gegen das Zugangscookie (Notweg).
    assert _start(_anfrage(seiten, "/lernen/api/kurse", cookie_kopf=f"{COOKIE}={_gut()}; {b}"))["status"] == 200
    # Nur das Zugangscookie: die Inhaber-Kennung aus der Umgebung.
    assert _start(_anfrage(seiten, "/lernen/api/kurse", cookie=_gut()))["status"] == 200
    # Ein ungueltiges Personen-Cookie neben einem gueltigen Zugangscookie: Notweg.
    kaputt = f"{PERSON_COOKIE}=muell"
    assert _start(_anfrage(seiten, "/lernen/api/kurse", cookie_kopf=f"{kaputt}; {COOKIE}={_gut()}"))["status"] == 200
    assert api.personen == ["lh-1-person-a", "lh-1-person-b", "lh-1-test", "lh-1-test"]
    assert seite.personen == ["lh-1-person-b"]
    # Die Flaechen-Weiche nimmt beide Cookies an.
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie_kopf=a))["status"] == 200
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie=_gut()))["status"] == 200
    assert durch.personen == ["lh-1-person-a", "lh-1-test"]


def test_ohne_gueltiges_cookie_403_und_kein_actor() -> None:
    seiten, flaeche, seite, api, durch = _weichen_lh()
    abgelaufen = f"{PERSON_COOKIE}={person_cookie(SCHLUESSEL, 'lh-1-person-a', jetzt=1_000_000)}"
    for kopf in (f"{PERSON_COOKIE}=muell", abgelaufen, f"{PERSON_COOKIE}=é",
                 f"{PERSON_COOKIE}={_uebergabe_token('lh-1-person-a', KURS_B)}"):
        assert _start(_anfrage(seiten, "/lernen/api/kurse", cookie_kopf=kopf))["status"] == 403, kopf
        assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie_kopf=kopf))["status"] == 403, kopf
    assert api.aufrufe == [] and durch.aufrufe == []


def test_ohne_learnhouse_url_nur_der_notweg() -> None:
    seiten, flaeche, seite, api, durch = _weichen_lh(E)
    # Die Uebergabe gibt es dann nicht: ohne Cookie 403 wie jede andere Seite, kein Personen-Cookie.
    gesendet = _anfrage(seiten, f"/lernen/uebergabe/{_uebergabe_token('lh-1-person-a', KURS_B)}")
    assert _start(gesendet)["status"] == 403
    assert all(k != b"set-cookie" for k, _ in _start(gesendet)["headers"])
    # Ein Personen-Cookie gilt nicht; das Zugangscookie wie bisher mit der Inhaber-Kennung.
    assert _start(_anfrage(seiten, "/lernen/api/kurse", cookie_kopf=_person("lh-1-person-a")))["status"] == 403
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie_kopf=_person("lh-1-person-a")))["status"] == 403
    kopf = f"{_person('lh-1-person-a')}; {COOKIE}={_gut()}"
    assert _start(_anfrage(seiten, "/lernen/api/kurse", cookie_kopf=kopf))["status"] == 200
    assert api.personen == ["lh-1-test"]


# ---- Flaechen-Port: Personen nur mit Katalog (Abschluss-Korrektur) ---------------------------------------

WS_ZU = [{"type": "websocket.close", "code": 1008}]


def test_flaeche_entfernte_person_403_http_und_websocket() -> None:
    katalog = FakeKatalog()
    katalog.entfernt.add("lh-1-person-a")
    _, flaeche, _, _, durch = _weichen_lh(katalog=katalog)
    gesendet = _anfrage(flaeche, "/", host=FLAECHE_HOST, cookie_kopf=_person("lh-1-person-a"))
    assert _start(gesendet)["status"] == 403
    assert gesendet[1]["body"].decode() == KEIN_ZUGANG_MEHR
    for pfad in ("/api/ai/command", "/api/canvases"):
        assert _start(_anfrage(flaeche, pfad, host=FLAECHE_HOST, methode="POST",
                               cookie_kopf=_person("lh-1-person-a")))["status"] == 403, pfad
    assert _anfrage(flaeche, "/api/mcp/canvas", art="websocket", host=FLAECHE_HOST,
                    cookie_kopf=_person("lh-1-person-a")) == WS_ZU
    assert durch.aufrufe == []
    assert set(katalog.fragen) == {"lh-1-person-a"}


def test_flaeche_gueltige_person_geht_durch() -> None:
    katalog = FakeKatalog()
    katalog.entfernt.add("lh-1-person-b")  # eine andere Person ist entfernt, A nicht
    _, flaeche, _, _, durch = _weichen_lh(katalog=katalog)
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie_kopf=_person("lh-1-person-a")))["status"] == 200
    assert _anfrage(flaeche, "/api/mcp/canvas", art="websocket", host=FLAECHE_HOST,
                    cookie_kopf=_person("lh-1-person-a"))[0]["status"] == 200
    assert durch.personen == ["lh-1-person-a", "lh-1-person-a"]
    assert katalog.fragen == ["lh-1-person-a", "lh-1-person-a"]


def test_flaeche_learnhouse_weg_laufende_arbeit_geht_weiter() -> None:
    # Wie die Controller-Regel der Seite: ein LearnHouse-Ausfall bricht laufende Arbeit nicht ab.
    katalog = FakeKatalog()
    katalog.weg = True
    _, flaeche, _, _, durch = _weichen_lh(katalog=katalog)
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie_kopf=_person("lh-1-person-a")))["status"] == 200
    assert _anfrage(flaeche, "/ws", art="websocket", host=FLAECHE_HOST,
                    cookie_kopf=_person("lh-1-person-a"))[0]["status"] == 200
    assert durch.personen == ["lh-1-person-a", "lh-1-person-a"]


def test_flaeche_notweg_ohne_katalog_frage() -> None:
    katalog = FakeKatalog()
    katalog.entfernt.add("lh-1-test")
    katalog.weg = True
    _, flaeche, _, _, durch = _weichen_lh(katalog=katalog)
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie=_gut()))["status"] == 200
    # Ungueltiges Personen-Cookie neben dem Zugangscookie: Notweg wie bisher, ohne Katalog.
    kopf = f"{PERSON_COOKIE}=muell; {COOKIE}={_gut()}"
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie_kopf=kopf))["status"] == 200
    assert durch.personen == ["lh-1-test", "lh-1-test"]
    assert katalog.fragen == []
    # Die oeffentlichen PenEcho-Dateien brauchen weder Cookie noch Katalog.
    assert _start(_anfrage(flaeche, "/widget-renderer.js", host=FLAECHE_HOST))["status"] == 200
    assert katalog.fragen == []


def test_flaeche_ohne_katalog_keine_personen() -> None:
    # Fail-closed: eine Flaechen-Weiche ohne Katalog laesst nur den Notweg herein.
    durch = Aufzeichner("durch", [])
    flaeche = FlaechenWeiche(E_LH, durch)
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie_kopf=_person("lh-1-person-a")))["status"] == 403
    assert _start(_anfrage(flaeche, "/", host=FLAECHE_HOST, cookie=_gut()))["status"] == 200
    assert durch.personen == ["lh-1-test"]


# ---- Teil C: Gruppen-Link und Gruppenrouten --------------------------------------------------------------

E_GRUPPEN = einstellung_lesen({
    "LEARNING_HUELLE_BASIS": f"https://{HOST}",
    "LEARNING_HUELLE_FLAECHE_BASIS": f"https://{FLAECHE_HOST}",
    "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
    "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
    "LEARNING_HUELLE_ACTOR": "lh-1-test",
    "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": "11111111-1111-4111-8111-111111111111", "titel": "T"}]),
    "LEARNING_HUELLE_LEARNHOUSE_URL": "http://learnhouse.test",
    "LEARNING_HUELLE_ABGLEICH_URL": "http://abgleich.test:8160",
    "LEARNING_HUELLE_ABGLEICH_SCHLUESSEL": "a" * 40,
})


def _ziel(gesendet: list[dict[str, Any]]) -> str:
    html = gesendet[1]["body"].decode("utf-8")
    anfang = html.index('content="0;url=') + len('content="0;url=')
    return html[anfang:html.index('"', anfang)].replace("&amp;", "&")


def test_uebergabe_mit_gruppe_leitet_weiter() -> None:
    seiten, *_ = _weichen_lh(E_GRUPPEN)
    token = _uebergabe_token("lh-1-person-a", KURS_B)
    gesendet = _anfrage(seiten, f"/lernen/uebergabe/{token}", query=b"gruppe=7")
    assert _start(gesendet)["status"] == 200
    assert _ziel(gesendet) == f"/lernen/?kurs={KURS_B}&gruppe=7"
    html = gesendet[1]["body"].decode("utf-8")
    assert f'<a href="/lernen/?kurs={KURS_B}&amp;gruppe=7">Weiter zum Lernen</a>' in html
    assert _ziel(_anfrage(seiten, f"/lernen/uebergabe/{token}", query=b"gruppe=123456789")) == (
        f"/lernen/?kurs={KURS_B}&gruppe=123456789")


def test_uebergabe_andere_gruppenwerte_werden_ignoriert() -> None:
    seiten, *_ = _weichen_lh(E_GRUPPEN)
    token = _uebergabe_token("lh-1-person-a", KURS_B)
    for query in (b"gruppe=", b"gruppe=1234567890", b"gruppe=7a", b"gruppe=-7", b"gruppe=%37%0A",
                  b"gruppe=7&gruppe=8", b"gruppe=%D9%A3", b"gruppe=7%22%3E", b"x=1", b""):
        gesendet = _anfrage(seiten, f"/lernen/uebergabe/{token}", query=query)
        assert _start(gesendet)["status"] == 200, query
        assert _ziel(gesendet) == f"/lernen/?kurs={KURS_B}", query
    # Ohne Kurs keine Gruppe: die Seite ohne Vorwahl.
    assert _ziel(_anfrage(seiten, f"/lernen/uebergabe/{_uebergabe_token('lh-1-person-a', None)}",
                          query=b"gruppe=7")) == "/lernen/"


def test_gruppen_aus_ohne_einstellung_404() -> None:
    kopf = f"{_person('lh-1-person-a')}"
    for einstellung in (E_LH, E):  # ohne Abgleich-Einstellung bzw. ganz ohne LearnHouse
        seiten, _, _, api, _ = _weichen_lh(einstellung)
        for pfad in ("/lernen/api/gruppen/7/oeffnen", "/lernen/api/gruppen", "/lernen/api/gruppen/"):
            gesendet = _anfrage(seiten, pfad, methode="POST", origin=f"https://{HOST}",
                                cookie_kopf=kopf if einstellung is E_LH else f"{COOKIE}={_gut()}")
            assert _start(gesendet)["status"] == 404, pfad
            assert json.loads(gesendet[1]["body"]) == {"meldung": GRUPPE_WEG}
        assert api.aufrufe == []
        # Andere API-Routen wie bisher
        assert _start(_anfrage(seiten, "/lernen/api/kurse", cookie=_gut()))["status"] == 200


def test_gruppenroute_mit_einstellung_geht_an_die_api() -> None:
    seiten, _, _, api, _ = _weichen_lh(E_GRUPPEN)
    gesendet = _anfrage(seiten, "/lernen/api/gruppen/7/oeffnen", methode="POST", origin=f"https://{HOST}",
                        cookie_kopf=_person("lh-1-person-a"))
    assert _start(gesendet)["status"] == 200
    assert api.personen == ["lh-1-person-a"]
    # Ohne Cookie bleibt es 403, auch bei abgeschalteten Gruppen.
    seiten_aus, *_ = _weichen_lh(E_LH)
    assert _start(_anfrage(seiten_aus, "/lernen/api/gruppen/7/oeffnen", methode="POST"))["status"] == 403
