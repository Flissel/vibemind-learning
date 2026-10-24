"""The local approval page: only a person in a browser on this machine can publish."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest

from spaces.learning.claw import werkzeuge
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
