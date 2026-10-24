"""The local review page: only a person in a browser on this machine decides a review."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

from spaces.learning.claw import werkzeuge
from spaces.learning.claw.freigabe_seite import FreigabeSeite
from spaces.learning.claw.pruefung_seite import PruefungSeite, PruefungStore
from spaces.learning.claw.server import BearerAuth, FreigabeWeiche

PORT = 8139
ORIGIN = f"http://127.0.0.1:{PORT}"
REVIEW = str(uuid4())


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[PruefungStore]:
    s = PruefungStore()
    monkeypatch.setattr(werkzeuge, "_pruefungen", s)
    yield s


class _Api:
    """Stands in for the Learning API behind LearningClient."""

    def __init__(self) -> None:
        self.decisions: list[tuple[str, dict]] = []

    def get(self, path: str, *, roles: str) -> dict:
        assert roles == "learning.reviewer"
        entry = {"review_id": REVIEW, "reason": "low_confidence", "task_prompt": "Zeichne <Zwinge>",
                 "answer_text": None, "expected_answer": "Werkstueck in Zwinge",
                 "rubric": [{"description": "Zwinge sichtbar", "points": 1}],
                 "ai_score": 0.9, "ai_confidence": 0.55}
        if path == "/reviews":
            return {"reviews": [entry]}
        return {**entry, "drawing_png_base64": base64.b64encode(b"\x89PNG").decode()}

    def post(self, path: str, body: dict, *, roles: str) -> dict:
        assert roles == "learning.reviewer"
        self.decisions.append((path, body))
        return {"review_id": REVIEW, "decision": body["decision"],
                "score": body.get("score", 0.9), "mastery_applied": True}

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> _Api:
    fake = _Api()
    monkeypatch.setattr(werkzeuge, "_client", lambda: fake)
    return fake


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(werkzeuge, "_browser_oeffnen", lambda url: urls.append(url) or True)
    return urls


async def _mcp(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"mcp"})


def _call(method: str, path: str, *, client: str = "127.0.0.1", headers=None, data=None):
    app = FreigabeWeiche(BearerAuth(_mcp, "t" * 40), FreigabeSeite(PORT), None, PruefungSeite(PORT))
    transport = httpx.ASGITransport(app=app, client=(client, 50000))

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as http:
            return await http.request(method, path, headers=headers or {}, data=data)

    return asyncio.run(go())


def _secret(opened: list[str]) -> str:
    return opened[-1].rsplit("/", 1)[-1]


def test_the_tool_lists_reviews_without_the_secret_and_opens_the_page(store, api, opened) -> None:
    result = werkzeuge.pruefungen_anzeigen()

    assert result["ok"] is True
    daten = result["daten"]
    assert daten["offen"] == 1 and daten["pruefungen"][0]["zeichnung"] is True
    assert opened[0].startswith(f"http://127.0.0.1:{PORT}/pruefung/")
    assert _secret(opened) not in repr(daten)


def test_the_page_shows_task_drawing_and_ai_result_escaped(store, api, opened) -> None:
    werkzeuge.pruefungen_anzeigen()
    page = _call("GET", f"/pruefung/{_secret(opened)}")

    assert page.status_code == 200
    assert "Zeichne &lt;Zwinge&gt;" in page.text and "<Zwinge>" not in page.text
    assert 'src="data:image/png;base64,' in page.text
    assert "90 % bei 55 % Sicherheit" in page.text
    assert "img-src data:" in page.headers["content-security-policy"]


def test_a_person_overrides_once_with_own_points(store, api, opened) -> None:
    werkzeuge.pruefungen_anzeigen()
    secret = _secret(opened)
    form = {"review_id": REVIEW, "entscheidung": "override", "punkte": "40", "notiz": "Zwinge fehlt"}

    first = _call("POST", f"/pruefung/{secret}", headers={"Origin": ORIGIN}, data=form)
    again = _call("POST", f"/pruefung/{secret}", headers={"Origin": ORIGIN}, data=form)

    assert first.status_code == 200 and "40 %" in first.text
    assert again.status_code == 409
    assert api.decisions == [(f"/reviews/{REVIEW}/decision",
                              {"decision": "override", "score": 0.4, "note": "Zwinge fehlt"})]


@pytest.mark.parametrize("origin", [None, "http://evil.example", "null"])
def test_cross_site_or_missing_origin_cannot_decide(store, api, opened, origin) -> None:
    werkzeuge.pruefungen_anzeigen()
    headers = {"Origin": origin} if origin else {}
    response = _call("POST", f"/pruefung/{_secret(opened)}", headers=headers,
                     data={"review_id": REVIEW, "entscheidung": "confirm"})
    assert response.status_code == 403 and api.decisions == []


def test_non_loopback_or_foreign_host_is_refused(store, api, opened) -> None:
    werkzeuge.pruefungen_anzeigen()
    secret = _secret(opened)
    assert _call("GET", f"/pruefung/{secret}", client="192.168.1.20").status_code == 403
    assert _call("GET", f"/pruefung/{secret}", headers={"Host": f"evil.example:{PORT}"}).status_code == 403


def test_bad_points_are_refused_before_anything_is_sent(store, api, opened) -> None:
    werkzeuge.pruefungen_anzeigen()
    response = _call("POST", f"/pruefung/{_secret(opened)}", headers={"Origin": ORIGIN},
                     data={"review_id": REVIEW, "entscheidung": "override", "punkte": "150"})
    assert response.status_code == 409 and api.decisions == []
