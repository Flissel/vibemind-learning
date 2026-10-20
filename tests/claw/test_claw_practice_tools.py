"""Practice and canvas tools over the Learning MCP endpoint."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest

from spaces.learning.claw import werkzeuge
from spaces.learning.claw.learning_client import LearningClient

KEY = "k" * 40
API = "http://127.0.0.1:8090/api/v1"
MCP = "http://127.0.0.1:8091/mcp"
COURSE = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
ACTIVITY = "33333333-3333-4333-8333-333333333333"
EVALUATION = "44444444-4444-4444-8444-444444444444"
TASK = "55555555-5555-4555-8555-555555555555"
REF = "bridge-ref"


class FakeMcp:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.error: dict[str, Any] | None = None
        self.status = 200
        self.payload: dict[str, Any] = {"state": "completed", "result": {"session": {"x": 1}}}

    @property
    def last_tool(self) -> str:
        return self.requests[-1]["params"]["name"]

    @property
    def last_event(self) -> dict[str, Any]:
        return self.requests[-1]["params"]["arguments"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status >= 500:
            return httpx.Response(self.status)
        body = json.loads(request.content)
        body["_timeout"] = request.extensions.get("timeout", {}).get("read")
        self.requests.append(body)
        if self.error is not None:
            payload, is_error = {"error": self.error}, True
        else:
            payload, is_error = self.payload, False
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": is_error,
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                },
            },
        )


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMcp]:
    f = FakeMcp()
    monkeypatch.setattr(
        werkzeuge,
        "_client",
        lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(f.handle)),
    )
    yield f


CALLS = [
    (lambda: werkzeuge.sitzung_starten(COURSE, 2, "training"), "learning_session_start"),
    (lambda: werkzeuge.naechste_aufgabe(SESSION), "learning_task_next"),
    (lambda: werkzeuge.antwort_geben(SESSION, 1, {"choice_id": "a"}), "learning_task_answer"),
    (lambda: werkzeuge.hinweis(SESSION, TASK), "learning_hint_request"),
    (lambda: werkzeuge.tutor_fragen(COURSE, SESSION, "Warum?"), "learning_tutor_ask"),
    (lambda: werkzeuge.fortschritt(SESSION), "learning_progress_show"),
    (lambda: werkzeuge.zeichenaufgabe_oeffnen(SESSION, ACTIVITY, REF), "learning_canvas_open"),
    (
        lambda: werkzeuge.zeichenhinweis(SESSION, ACTIVITY, 3, REF, ["o1"]),
        "learning_canvas_hint",
    ),
    (lambda: werkzeuge.bewertung_zeigen(EVALUATION, SESSION, REF), "learning_canvas_review"),
]


@pytest.mark.parametrize("call, tool", CALLS)
def test_tool_maps_to_exact_learning_mcp_tool(fake_mcp: FakeMcp, call: Any, tool: str) -> None:
    out = call()
    assert out["ok"] is True and fake_mcp.last_tool == tool
    assert out["daten"] == {"zustand": "completed", "ergebnis": {"session": {"x": 1}}, "beleg": None}


def test_result_carries_state_and_evidence(fake_mcp: FakeMcp) -> None:
    evidence = {"evidence_id": "ev-1", "evidence_type": "application_readback"}
    fake_mcp.payload = {"state": "completed", "result": {"hint": "h"}, "evidence": evidence}
    out = werkzeuge.hinweis(SESSION, TASK)
    assert out == {
        "ok": True,
        "daten": {"zustand": "completed", "ergebnis": {"hint": "h"}, "beleg": evidence},
    }


@pytest.mark.parametrize("payload", [{"state": "accepted"}, {"state": "completed", "result": "x"}])
def test_missing_result_is_unavailable_not_a_fallback(
    fake_mcp: FakeMcp, payload: dict[str, Any]
) -> None:
    fake_mcp.payload = payload
    out = werkzeuge.fortschritt(SESSION)
    assert out["ok"] is False and "Ergebnis" in out["fehler"]


@pytest.mark.parametrize("code", ["session_revision_conflict", "canvas_revision_conflict"])
def test_revision_conflicts_become_reload_hint(fake_mcp: FakeMcp, code: str) -> None:
    fake_mcp.error = {"code": code, "message": "stale"}
    out = werkzeuge.antwort_geben(SESSION, 1, {"choice_id": "a"})
    assert out == {"ok": False, "fehler": "Der Stand hat sich geaendert, bitte neu laden"}


def test_envelope_shape_and_actor(fake_mcp: FakeMcp) -> None:
    werkzeuge.sitzung_starten(COURSE, 2, "exam")
    e = fake_mcp.last_event
    assert e["event_type"] == "learning.session.start"
    assert e["actor"] == {"actor_id": "learning-claw", "actor_type": "local_user"}
    assert e["course_id"] == COURSE
    assert e["payload"]["course_revision"] == 2 and e["payload"]["mode"] == "exam"
    assert e["payload"]["blueprint"] == {"max_attempts": 3}
    assert UUID(e["invocation_id"]) and UUID(e["correlation_id"])


def test_write_events_carry_idempotency_keys(fake_mcp: FakeMcp) -> None:
    werkzeuge.sitzung_starten(COURSE, 2, "training")
    first = fake_mcp.last_event["idempotency_key"]
    werkzeuge.sitzung_starten(COURSE, 2, "training")
    assert first and fake_mcp.last_event["idempotency_key"] == first
    werkzeuge.antwort_geben(SESSION, 4, {"choice_id": "a"})
    e = fake_mcp.last_event
    assert e["idempotency_key"] and e["session_id"] == SESSION and e["expected_revision"] == 4
    assert e["payload"] == {"answer": {"choice_id": "a"}}
    werkzeuge.antwort_geben(SESSION, 4, {"choice_id": "b"})
    assert fake_mcp.last_event["idempotency_key"] != e["idempotency_key"]


def test_read_events_have_no_idempotency_key(fake_mcp: FakeMcp) -> None:
    werkzeuge.naechste_aufgabe(SESSION)
    assert fake_mcp.last_event.get("idempotency_key") is None
    assert fake_mcp.last_event["session_id"] == SESSION


def test_canvas_payloads(fake_mcp: FakeMcp) -> None:
    werkzeuge.zeichenaufgabe_oeffnen(SESSION, ACTIVITY, REF)
    assert fake_mcp.last_event["payload"] == {"activity_id": ACTIVITY, "session_ref": REF}
    werkzeuge.zeichenhinweis(SESSION, ACTIVITY, 3, REF, ["o1", "o2"])
    e = fake_mcp.last_event
    assert e["expected_revision"] == 3 and e["session_id"] == SESSION
    assert e["payload"]["selection"] == {"object_ids": ["o1", "o2"]}
    assert e["payload"]["session_ref"] == REF
    werkzeuge.bewertung_zeigen(EVALUATION, SESSION, REF)
    e = fake_mcp.last_event
    assert e["payload"] == {"evaluation_id": EVALUATION, "session_ref": REF}
    assert e["idempotency_key"]  # canvas_review is a write event


def test_tutor_and_hint_payloads(fake_mcp: FakeMcp) -> None:
    werkzeuge.tutor_fragen(COURSE, SESSION, "Warum?")
    e = fake_mcp.last_event
    assert e["payload"] == {"question": "Warum?"} and e["session_id"] == SESSION
    assert e["course_id"] == COURSE
    werkzeuge.hinweis(SESSION, TASK)
    assert fake_mcp.last_event["payload"] == {"task_id": TASK}


def test_mcp_error_result_is_fail_soft(fake_mcp: FakeMcp) -> None:
    fake_mcp.error = {"code": "session_revision_conflict", "message": "stale"}
    out = werkzeuge.naechste_aufgabe(SESSION)
    assert out["ok"] is False and out["fehler"]
    assert KEY not in json.dumps(out)


def test_unavailable_is_fail_soft(fake_mcp: FakeMcp) -> None:
    fake_mcp.status = 503
    out = werkzeuge.fortschritt(SESSION)
    assert out["ok"] is False and "erreichbar" in out["fehler"]


def test_invalid_ids_are_fail_soft(fake_mcp: FakeMcp) -> None:
    out = werkzeuge.naechste_aufgabe("not-a-uuid")
    assert out["ok"] is False and not fake_mcp.requests


def test_no_canvas_save_or_submit_tool_exists() -> None:
    names = dir(werkzeuge)
    forbidden = ("zeichnung_speichern", "zeichnung_abgeben", "canvas_save", "canvas_submit")
    assert not any(n in names for n in forbidden)
    assert not any("speichern" in n or "abgeben" in n for n in names if n.startswith("zeichn"))


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


def test_sitzung_starten_window_reuses_then_renews_the_key(
    fake_mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(werkzeuge, "_fenster", werkzeuge.Idempotenzfenster(clock=clock))
    werkzeuge.sitzung_starten(COURSE, 2, "training")
    first = fake_mcp.last_event["idempotency_key"]
    clock.now += 599
    werkzeuge.sitzung_starten(COURSE, 2, "training")
    assert fake_mcp.last_event["idempotency_key"] == first
    clock.now += 2
    werkzeuge.sitzung_starten(COURSE, 2, "training")
    second = fake_mcp.last_event["idempotency_key"]
    assert second != first and second.startswith("claw-")


def test_antwort_geben_key_stays_deterministic(
    fake_mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(werkzeuge, "_fenster", werkzeuge.Idempotenzfenster(clock=clock))
    werkzeuge.antwort_geben(SESSION, 4, {"choice_id": "a"})
    first = fake_mcp.last_event["idempotency_key"]
    clock.now += 10_000
    werkzeuge.antwort_geben(SESSION, 4, {"choice_id": "a"})
    assert fake_mcp.last_event["idempotency_key"] == first


def test_model_backed_tools_wait_longer_than_reads(fake_mcp: FakeMcp) -> None:
    # Live, grading and tutor answers take 13-60 s through OpenFang; the 30 s
    # client timeout reported "Learning ist nicht erreichbar" while the call ran.
    werkzeuge.antwort_geben(SESSION, 1, {"text": "x"})
    werkzeuge.tutor_fragen(COURSE, SESSION, "Warum?")
    werkzeuge.zeichenhinweis(SESSION, ACTIVITY, 1, REF, ["o1"])
    werkzeuge.naechste_aufgabe(SESSION)

    timeouts = [request["_timeout"] for request in fake_mcp.requests]
    assert timeouts[:3] == [werkzeuge.MODEL_TIMEOUT_SECONDS] * 3
    assert timeouts[3] < werkzeuge.MODEL_TIMEOUT_SECONDS
    assert werkzeuge.MODEL_TIMEOUT_SECONDS > 630
