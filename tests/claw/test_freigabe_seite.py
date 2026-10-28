"""The local approval page: only a person in a browser on this machine can publish."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest

from spaces.learning.claw import server as server_module, werkzeuge
from spaces.learning.claw.freigabe import FreigabeAbgelehnt, FreigabeStore
from spaces.learning.claw.freigabe_seite import FreigabeSeite
from spaces.learning.claw.server import BearerAuth, FreigabeWeiche

PORT = 8139
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[FreigabeStore]:
    s = FreigabeStore()
    monkeypatch.setattr(werkzeuge, "_freigaben", s)
    yield s


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_freigeben(geheimnis: str) -> dict[str, object]:
        try:
            anzeige = werkzeuge._freigaben.einloesen(geheimnis)
        except FreigabeAbgelehnt as error:
            return {"ok": False, "fehler": error.grund}
        calls.append(anzeige.job_id)
        return {"ok": True, "daten": {"job_id": anzeige.job_id, "zustand": "published"}}

    monkeypatch.setattr(werkzeuge, "freigeben", fake_freigeben)
    return calls


async def _mcp(scope, receive, send) -> None:  # stands in for the MCP app
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"mcp"})


def _call(method: str, path: str, *, client: str = "127.0.0.1", headers=None) -> httpx.Response:
    app = FreigabeWeiche(BearerAuth(_mcp, "t" * 40), FreigabeSeite(PORT))
    transport = httpx.ASGITransport(app=app, client=(client, 50000))

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as http:
            return await http.request(method, path, headers=headers or {})

    return asyncio.run(go())


def _pending(store: FreigabeStore, titel: str = "Akku sicher") -> str:
    return store.merken(
        "job-1",
        8,
        3,
        {
            "job_id": "job-1",
            "titel": titel,
            "kapitel": [{"titel": "Vor der Arbeit", "lektionen": ["Check"], "aufgaben": 3}],
            "unbelegte_aussagen": [],
            "revision": 8,
            "kurs_revision": 3,
        },
    ).geheimnis


def test_page_shows_the_outline_and_a_button(store) -> None:
    secret = _pending(store)
    page = _call("GET", f"/freigabe/{secret}")
    assert page.status_code == 200
    assert "Akku sicher" in page.text and "Vor der Arbeit" in page.text
    assert '<form method="post">' in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    # Live (24.10.): with "no-referrer" Chrome sent "Origin: null" on the button's
    # form post and the real click was refused. "same-origin" still keeps the
    # secret link from other sites and lets the browser send this page's Origin.
    assert page.headers["referrer-policy"] == "same-origin"


def test_titles_are_escaped(store) -> None:
    secret = _pending(store, titel="<script>alert(1)</script>")
    page = _call("GET", f"/freigabe/{secret}")
    assert "<script>" not in page.text and "&lt;script&gt;" in page.text


def test_button_with_own_origin_publishes_once(store, published) -> None:
    secret = _pending(store)
    first = _call("POST", f"/freigabe/{secret}", headers={"Origin": ORIGIN})
    again = _call("POST", f"/freigabe/{secret}", headers={"Origin": ORIGIN})
    assert first.status_code == 200 and "Veroeffentlicht" in first.text
    assert again.status_code != 200
    assert published == ["job-1"]


@pytest.mark.parametrize("origin", [None, "http://evil.example", "null"])
def test_cross_site_or_missing_origin_cannot_publish(store, published, origin) -> None:
    secret = _pending(store)
    headers = {"Origin": origin} if origin else {}
    response = _call("POST", f"/freigabe/{secret}", headers=headers)
    assert response.status_code == 403
    assert published == []


def test_non_loopback_client_or_foreign_host_is_refused(store, published) -> None:
    secret = _pending(store)
    remote = _call("GET", f"/freigabe/{secret}", client="192.168.1.20")
    rebound = _call("GET", f"/freigabe/{secret}", headers={"Host": f"evil.example:{PORT}"})
    assert remote.status_code == 403 and rebound.status_code == 403


def test_unknown_or_malformed_secret(store) -> None:
    assert _call("GET", "/freigabe/" + "a" * 32).status_code == 410
    assert _call("GET", "/freigabe/short").status_code == 404
    # A traversal is normalized away before routing and lands behind the bearer check.
    assert _call("GET", "/freigabe/../etc").status_code == 401


def test_mcp_path_still_requires_the_bearer_token(store) -> None:
    assert _call("POST", "/mcp").status_code == 401
    ok = _call("POST", "/mcp", headers={"Authorization": "Bearer " + "t" * 40})
    assert ok.status_code == 200 and ok.text == "mcp"


def test_page_lists_the_non_blocking_ai_notes_escaped(store) -> None:
    secret = store.merken(
        "job-2", 8, 3,
        {"job_id": "job-2", "titel": "Akku", "kapitel": [], "unbelegte_aussagen": [],
         "ki_hinweise": ["Kapiteltitel <lang>"], "revision": 8, "kurs_revision": 3},
    ).geheimnis
    page = _call("GET", f"/freigabe/{secret}")
    assert "Hinweise der KI-Pruefung" in page.text
    assert "Kapiteltitel &lt;lang&gt;" in page.text


# Werkstatt release: comparison and findings, button only when nothing blocks; also from the
# phone through `tailscale serve` (client stays loopback, Host/Origin are the configured base).

BASIS = "https://pc.example.ts.net"
HANDY_HOST = "pc.example.ts.net"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _ohne_handy_basis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEARNING_CLAW_FREIGABE_BASIS", raising=False)


@pytest.fixture
def uhr(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(werkzeuge, "_freigaben", FreigabeStore(clock=clock))
    return clock


@pytest.fixture
def werkstatt_freigegeben(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(geheimnis: str) -> dict[str, object]:
        try:
            anzeige = werkzeuge._freigaben.einloesen(geheimnis, art="werkstatt")
        except FreigabeAbgelehnt as error:
            return {"ok": False, "fehler": error.grund}
        calls.append(anzeige.job_id)
        return {"ok": True, "daten": {
            "learnhouse_revision": 8,
            "katalog": {"items": 4, "konzepte": 2, "lernstand_uebernommen": 1},
        }}

    monkeypatch.setattr(werkzeuge, "werkstatt_freigeben", fake)
    return calls


def _werkstatt(blockiert: bool = False, titel: str = "PowerShell Grundlagen") -> str:
    eintrag = {"task_id": "t1", "kapitel": "K1", "summary": "Dateien <pruefen>",
               "fassung_alt": 2, "fassung_neu": 4}
    return werkzeuge._freigaben.merken(
        "f-1", 3, 7,
        {
            "art": "werkstatt", "course_id": "kurs-1", "titel": titel, "nummer": 3,
            "learnhouse_revision": 7, "blockiert": blockiert, "nichts_zu_veroeffentlichen": False,
            "vergleich": {
                "neu": [], "geaendert": [eintrag],
                "textkorrektur": [{**eintrag, "summary": "Ordner wechseln"}],
                "geloescht": [{**eintrag, "summary": "Alte Aufgabe", "fassung_neu": None}],
                "unveraendert": 5,
            },
            "befunde": [{"code": "address_mixed", "task_id": None, "blockierend": blockiert,
                         "text": "Der Kurs mischt Du und Sie."}],
        },
        art="werkstatt",
    ).geheimnis


def test_werkstatt_seite_zeigt_vergleich_und_befunde(uhr) -> None:
    secret = _werkstatt()
    page = _call("GET", f"/freigabe/{secret}")
    assert page.status_code == 200
    for text in ("PowerShell Grundlagen", "Geändert", "Textkorrektur", "Gelöscht",
                 "Dateien &lt;pruefen&gt;", "Fassung 2 → 4", "Ordner wechseln", "Alte Aufgabe",
                 "Der Kurs mischt Du und Sie.", "Unverändert: 5"):
        assert text in page.text, text
    assert '<button type="submit"' in page.text
    assert 'name="viewport"' in page.text


def test_blockierte_werkstatt_seite_hat_keinen_aktiven_knopf(uhr, werkstatt_freigegeben) -> None:
    secret = _werkstatt(blockiert=True)
    page = _call("GET", f"/freigabe/{secret}")
    assert page.status_code == 200
    assert "blockiert" in page.text and "disabled" in page.text
    assert 'type="submit"' not in page.text
    post = _call("POST", f"/freigabe/{secret}", headers={"Origin": ORIGIN})
    assert post.status_code == 409
    assert werkstatt_freigegeben == []


def test_freigabe_seite_gilt_einmal(uhr, werkstatt_freigegeben, published) -> None:
    secret = _werkstatt()
    first = _call("POST", f"/freigabe/{secret}", headers={"Origin": ORIGIN})
    again = _call("POST", f"/freigabe/{secret}", headers={"Origin": ORIGIN})
    assert first.status_code == 200, first.text
    assert "Freigegeben" in first.text and "LearnHouse-Revision 8" in first.text
    assert "4 Aufgaben" in first.text
    assert again.status_code != 200
    assert werkstatt_freigegeben == ["f-1"] and published == []
    # After 10 minutes a fresh page is refused as well.
    spaet = _werkstatt()
    uhr.now += 601
    assert _call("GET", f"/freigabe/{spaet}").status_code == 410
    assert _call("POST", f"/freigabe/{spaet}", headers={"Origin": ORIGIN}).status_code != 200
    assert werkstatt_freigegeben == ["f-1"]


def test_werkstatt_seite_zeigt_katalogfehler(uhr, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = _werkstatt()

    def fake(geheimnis: str) -> dict[str, object]:
        werkzeuge._freigaben.einloesen(geheimnis, art="werkstatt")
        return {"ok": True, "daten": {"learnhouse_revision": 8, "katalog_fehler": "OperationalError"}}

    monkeypatch.setattr(werkzeuge, "werkstatt_freigeben", fake)
    page = _call("POST", f"/freigabe/{secret}", headers={"Origin": ORIGIN})
    assert page.status_code == 200
    assert "LearnHouse-Revision 8" in page.text and "OperationalError" in page.text


def test_freigabe_seite_akzeptiert_konfigurierten_handy_host(
    uhr, werkstatt_freigegeben, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", BASIS)
    secret = _werkstatt()
    page = _call("GET", f"/freigabe/{secret}", headers={"Host": HANDY_HOST})
    assert page.status_code == 200
    post = _call("POST", f"/freigabe/{secret}", headers={"Host": HANDY_HOST, "Origin": BASIS})
    assert post.status_code == 200, post.text
    assert werkstatt_freigegeben == ["f-1"]


def test_fremder_host_bleibt_403(uhr, werkstatt_freigegeben, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = _werkstatt()
    # Without the setting the phone host is just another foreign host.
    assert _call("GET", f"/freigabe/{secret}", headers={"Host": HANDY_HOST}).status_code == 403
    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", BASIS)
    for host in ("evil.example", "other.example.ts.net", f"{HANDY_HOST}:8443", f"x{HANDY_HOST}"):
        assert _call("GET", f"/freigabe/{secret}", headers={"Host": host}).status_code == 403, host
    fremd = _call("POST", f"/freigabe/{secret}",
                  headers={"Host": HANDY_HOST, "Origin": "https://evil.example"})
    assert fremd.status_code == 403
    unsicher = _call("POST", f"/freigabe/{secret}",
                     headers={"Host": HANDY_HOST, "Origin": f"http://{HANDY_HOST}"})
    assert unsicher.status_code == 403
    entfernt = _call("GET", f"/freigabe/{secret}", client="100.64.0.9", headers={"Host": HANDY_HOST})
    assert entfernt.status_code == 403
    assert werkstatt_freigegeben == []


def test_werkstatt_seite_zeigt_was_offline_geht(uhr) -> None:
    secret = werkzeuge._freigaben.merken(
        "f-2", 4, 7,
        {
            "art": "werkstatt", "course_id": "kurs-1", "titel": "Kurs", "nummer": 4,
            "learnhouse_revision": 7, "blockiert": False, "nichts_zu_veroeffentlichen": False,
            "vergleich": {
                "neu": [], "geaendert": [], "textkorrektur": [], "unveraendert": 1,
                "geloescht": [{"task_id": "t1", "kapitel": "K1", "summary": "Alt",
                               "fassung_alt": 1, "fassung_neu": None}],
                "offline": [
                    {"key": "l1", "art": "lesson", "titel": "Lektion <alt>"},
                    {"key": "a1", "art": "task", "titel": "Frage alt"},
                ],
                "zusammengefuehrt": [{"key": "l2", "art": "lesson", "titel": "Lektion B"}],
            },
            "befunde": [],
        },
        art="werkstatt",
    ).geheimnis
    page = _call("GET", f"/freigabe/{secret}")
    assert page.status_code == 200
    assert "Geht offline: 3" in page.text
    assert "Lektion &lt;alt&gt;" in page.text and "<alt>" not in page.text
    assert "Frage alt" in page.text and "Lektion B" in page.text
    assert "zusammengeführt" in page.text
    assert '<button type="submit"' in page.text  # not blocking


def test_werkstatt_seite_ohne_offline_hat_keinen_abschnitt(uhr) -> None:
    page = _call("GET", f"/freigabe/{_werkstatt()}")
    assert "Geht offline" not in page.text


@pytest.mark.parametrize(
    ("basis", "host", "origin"),
    [
        ("https://PC.Example.ts.net", "pc.example.ts.net", "https://pc.example.ts.net"),
        ("https://pc.example.ts.net:443", "pc.example.ts.net", "https://pc.example.ts.net"),
        ("https://pc.example.ts.net", "PC.EXAMPLE.TS.NET", "https://PC.example.ts.net"),
        ("https://pc.example.ts.net:8443", "pc.example.ts.net:8443", "https://pc.example.ts.net:8443"),
    ],
)
def test_handy_host_ohne_gross_klein_und_ohne_443(
    uhr, werkstatt_freigegeben, monkeypatch: pytest.MonkeyPatch, basis: str, host: str, origin: str
) -> None:
    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", basis)
    secret = _werkstatt()
    assert _call("GET", f"/freigabe/{secret}", headers={"Host": host}).status_code == 200
    post = _call("POST", f"/freigabe/{secret}", headers={"Host": host, "Origin": origin})
    assert post.status_code == 200, post.text
    assert werkstatt_freigegeben == ["f-1"]


def test_handy_basis_mit_anderem_port_behaelt_den_port(uhr, monkeypatch: pytest.MonkeyPatch) -> None:
    from spaces.learning.claw.freigabe import handy_basis

    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", "https://PC.example.ts.net:8443/")
    assert handy_basis() == ("https://pc.example.ts.net:8443", "pc.example.ts.net:8443")
    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", "https://PC.example.ts.net:443")
    assert handy_basis() == ("https://pc.example.ts.net", "pc.example.ts.net")
    secret = _werkstatt()
    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", "https://pc.example.ts.net:8443")
    assert _call("GET", f"/freigabe/{secret}", headers={"Host": HANDY_HOST}).status_code == 403


def test_produktiver_uvicorn_umschreibt_den_client_nicht_per_forwarded_for(
    uhr, werkstatt_freigegeben, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind `tailscale serve` the peer is 127.0.0.1 and X-Forwarded-For names the phone."""
    import uvicorn
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", BASIS)
    secret = _werkstatt()
    app = FreigabeWeiche(BearerAuth(_mcp, "t" * 40), FreigabeSeite(PORT))
    config = uvicorn.Config(app, **server_module.uvicorn_settings("127.0.0.1", PORT))
    config.load()
    loaded = config.loaded_app
    assert not isinstance(loaded, ProxyHeadersMiddleware)

    transport = httpx.ASGITransport(app=loaded, client=("127.0.0.1", 50000))

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as http:
            return await http.get(
                f"/freigabe/{secret}",
                headers={"Host": HANDY_HOST, "x-forwarded-for": "100.84.127.84",
                         "x-forwarded-proto": "https", "x-forwarded-host": HANDY_HOST},
            )

    assert asyncio.run(go()).status_code == 200
