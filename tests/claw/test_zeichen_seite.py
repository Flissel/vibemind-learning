"""Local drawing page: the person draws and submits; guards like the approval page."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from spaces.learning.claw import werkzeuge
from spaces.learning.claw.server import BearerAuth, FreigabeWeiche
from spaces.learning.claw.freigabe_seite import FreigabeSeite
from spaces.learning.claw.zeichen_seite import ZeichenSeite
from spaces.learning.claw.zeichnung import (
    Zeichnung,
    ZeichnungAbgelehnt,
    ZeichnungStore,
    objekte_aus_strichen,
)

PORT = 8139
ORIGIN = f"http://127.0.0.1:{PORT}"
SESSION = "22222222-2222-4222-8222-222222222222"
ACTIVITY = "33333333-3333-4333-8333-333333333333"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nzeichnung").decode()
STRICHE = [{"punkte": [[10, 10], [60, 40], [120, 80]], "farbe": "#111111", "breite": 5}]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[ZeichnungStore]:
    s = ZeichnungStore()
    monkeypatch.setattr(werkzeuge, "_zeichnungen", s)
    yield s


@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    document = {"version": "1", "project_id": "11111111-1111-4111-8111-111111111111",
                "canvas_id": "44444444-4444-4444-8444-444444444444", "revision": 0,
                "background": "white", "objects": []}
    answers = {
        "learning_canvas_open": {"canvas": {"revision": 0, "document": document}},
        "learning_canvas_save": {"canvas": {"revision": 1}},
        "learning_canvas_submit": {
            "evaluation_id": "55555555-5555-4555-8555-555555555555",
            "evaluation": {"score": 0.75, "accepted": True, "criteria": [
                {"criterion_id": "zwinge", "score": 0.75, "feedback": "Zwinge erkennbar."}]},
        },
    }

    def fake_mcp(event_type, tool, **kwargs):
        calls.append((tool, kwargs))
        return {"ok": True, "daten": {"zustand": "completed", "ergebnis": answers[tool], "beleg": None}}

    def fake_progress(session_id):
        calls.append(("learning_progress_show", {"session_id": session_id}))
        return {"ok": True, "daten": {"ergebnis": {"session": {"session_revision": 7}}}}

    monkeypatch.setattr(werkzeuge, "_mcp", fake_mcp)
    monkeypatch.setattr(werkzeuge, "fortschritt", fake_progress)
    return calls


async def _app_mcp(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"mcp"})


def _call(method: str, path: str, *, client: str = "127.0.0.1", headers=None, body=None):
    app = FreigabeWeiche(BearerAuth(_app_mcp, "t" * 40), FreigabeSeite(PORT), ZeichenSeite(PORT))
    transport = httpx.ASGITransport(app=app, client=(client, 50000))

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as http:
            return await http.request(method, path, headers=headers or {}, content=body)

    return asyncio.run(go())


def _offen(store: ZeichnungStore) -> str:
    return store.merken(Zeichnung(SESSION, ACTIVITY, "z-ref", "Zeichne das gesicherte Werkstueck."))


def test_page_shows_the_task_and_a_nonce_protected_canvas(store) -> None:
    page = _call("GET", f"/zeichnen/{_offen(store)}")
    assert page.status_code == 200
    assert "gesicherte Werkstueck" in page.text and "<canvas" in page.text
    csp = page.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in page.text and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]


def test_only_local_and_known_secrets(store) -> None:
    secret = _offen(store)
    assert _call("GET", f"/zeichnen/{secret}", client="10.0.0.5").status_code == 403
    assert _call("GET", f"/zeichnen/{secret}", headers={"Host": "evil.example"}).status_code == 403
    assert _call("GET", "/zeichnen/" + "x" * 30).status_code == 410


def test_submit_needs_this_pages_origin(store, mcp) -> None:
    body = json.dumps({"png": PNG, "striche": STRICHE})
    for origin in (None, "null", "http://evil.example"):
        headers = {"content-type": "application/json", **({"Origin": origin} if origin else {})}
        assert _call("POST", f"/zeichnen/{_offen(store)}", headers=headers, body=body).status_code == 403
    assert mcp == []


def test_submit_saves_submits_once_and_shows_the_evaluation(store, mcp) -> None:
    secret = _offen(store)
    headers = {"content-type": "application/json", "Origin": ORIGIN}
    body = json.dumps({"png": PNG, "striche": STRICHE})

    first = _call("POST", f"/zeichnen/{secret}", headers=headers, body=body)
    again = _call("POST", f"/zeichnen/{secret}", headers=headers, body=body)

    assert first.status_code == 200, first.text
    assert first.json()["daten"]["punkte"] == 0.75
    assert first.json()["daten"]["kriterien"][0]["rueckmeldung"] == "Zwinge erkennbar."
    assert again.status_code == 409
    tools = [tool for tool, _ in mcp]
    assert tools == ["learning_canvas_open", "learning_canvas_save", "learning_progress_show",
                     "learning_canvas_submit"]
    save = mcp[1][1]
    assert save["expected_revision"] == 0 and save["payload"]["session_ref"] == "z-ref"
    assert save["payload"]["document"]["objects"][0]["object_type"] == "path"
    assert save["payload"]["snapshot_png_base64"] == PNG.removeprefix("data:image/png;base64,")
    submit = mcp[3][1]
    assert submit["expected_revision"] == 1 and submit["confirmation"]["confirmed"] is True
    assert submit["payload"]["adaptive_session_revision"] == 7


def test_empty_drawing_is_not_submitted(store, mcp) -> None:
    headers = {"content-type": "application/json", "Origin": ORIGIN}
    body = json.dumps({"png": PNG, "striche": []})
    assert _call("POST", f"/zeichnen/{_offen(store)}", headers=headers, body=body).status_code == 409
    assert mcp == []


def test_strokes_become_bounded_path_objects() -> None:
    objekte = objekte_aus_strichen(STRICHE + [{"punkte": []}, {"punkte": [[5, 5]]}])
    assert objekte[0] == {"object_id": "strich-1", "object_type": "path", "x": 10.0, "y": 10.0,
                          "width": 110.0, "height": 70.0, "rotation": 0}
    assert [o["object_id"] for o in objekte] == ["strich-1", "strich-3"]
    with pytest.raises(ZeichnungAbgelehnt):
        objekte_aus_strichen([{"punkte": [["a", 1]]}])


def test_hint_button_returns_the_section_hint_without_using_the_secret(store, monkeypatch) -> None:
    calls = []

    def fake_hinweis(session_id, task_id):
        calls.append((session_id, task_id))
        return {"ok": True, "daten": {"ergebnis": {"hint": "Lies den Abschnitt „Waehrend der Arbeit“."}}}

    monkeypatch.setattr(werkzeuge, "hinweis", fake_hinweis)
    secret = _offen(store)
    headers = {"Origin": ORIGIN}

    answer = _call("POST", f"/zeichnen/{secret}/hinweis", headers=headers, body=b"{}")

    assert answer.status_code == 200
    assert answer.json() == {"ok": True, "hinweis": "Lies den Abschnitt „Waehrend der Arbeit“."}
    assert calls == [(SESSION, ACTIVITY)]
    assert store.holen(secret)  # a hint does not use up the drawing task
    assert _call("POST", f"/zeichnen/{secret}/hinweis", body=b"{}").status_code == 403


def test_hint_button_explains_when_hints_are_off(store, monkeypatch) -> None:
    monkeypatch.setattr(werkzeuge, "hinweis",
                        lambda s, t: {"ok": False, "fehler": "exam sessions do not expose hints"})
    answer = _call("POST", f"/zeichnen/{_offen(store)}/hinweis", headers={"Origin": ORIGIN}, body=b"{}")
    assert answer.status_code == 409 and answer.json()["ok"] is False


def test_page_offers_a_hint_button(store) -> None:
    assert 'id="hinweis"' in _call("GET", f"/zeichnen/{_offen(store)}").text


def test_unsure_evaluation_is_marked_for_human_review(store, mcp, monkeypatch) -> None:
    # Live (24.10.): a drawing got 90 % at confidence 0.55; the page showed only
    # "Ergebnis: 90 %" although the decision went to human review.
    original = werkzeuge._mcp

    def unsure(event_type, tool, **kwargs):
        out = original(event_type, tool, **kwargs)
        if tool == "learning_canvas_submit":
            out["daten"]["ergebnis"]["evaluation"] = {
                "score": 0.9, "accepted": False, "confidence": 0.55, "criteria": []}
        return out

    monkeypatch.setattr(werkzeuge, "_mcp", unsure)
    headers = {"content-type": "application/json", "Origin": ORIGIN}
    body = json.dumps({"png": PNG, "striche": STRICHE})

    daten = _call("POST", f"/zeichnen/{_offen(store)}", headers=headers, body=body).json()["daten"]

    assert daten["angenommen"] is False and daten["sicherheit"] == 0.55
    assert "Mensch" in daten["hinweis_bewertung"]


def test_page_script_shows_the_review_note() -> None:
    from spaces.learning.claw import zeichen_seite

    assert "hinweis_bewertung" in zeichen_seite._SKRIPT
