"""Course tools of the Learning claw sidecar against a faked Learning API."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest

from spaces.learning.claw import learning_client, werkzeuge
from spaces.learning.claw.learning_client import (
    LearningClient,
    LearningRejected,
    idempotency_key,
)

KEY = "k" * 40
COURSE = "11111111-1111-4111-8111-111111111111"
SOURCE = "22222222-2222-4222-8222-222222222222"
JOB = "33333333-3333-4333-8333-333333333333"
API = "http://127.0.0.1:8090/api/v1"
MCP = "http://127.0.0.1:8091/mcp"
CORR = "44444444-4444-4444-8444-444444444444"


class Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.override: Callable[[httpx.Request], httpx.Response] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            return self.override(request)
        path, method = request.url.path, request.method
        if method == "POST" and path.endswith("/course-factory/jobs"):
            return httpx.Response(202, json=_job("queued", 1))
        if method == "GET" and path.endswith(f"/course-factory/jobs/{JOB}"):
            return httpx.Response(200, json=_job("review_pending", 4))
        if method == "POST" and path.endswith(f"/course-factory/jobs/{JOB}/review"):
            return httpx.Response(200, json=_job("rejected", 5))
        if method == "POST" and path.endswith("/materials/import"):
            return httpx.Response(
                202,
                json={
                    "correlation_id": CORR,
                    "outcome": "completed",
                    "source_id": SOURCE,
                    "revision": 1,
                    "artifact_id": "a1",
                    "projection_state": "pending",
                },
            )
        if method == "GET" and path.endswith(f"/materials/{SOURCE}"):
            return httpx.Response(
                200,
                json={
                    "source_id": SOURCE,
                    "course_id": COURSE,
                    "revision": 2,
                    "content_hash": "0" * 64,
                    "projection_state": "complete",
                },
            )
        if method == "GET" and path.endswith(f"/courses/{COURSE}"):
            return httpx.Response(
                200,
                json={
                    "correlation_id": CORR,
                    "outcome": "completed",
                    "course_id": COURSE,
                    "revision": 3,
                    "course": {"title": "Sicherheit"},
                },
            )
        return httpx.Response(404, json=_err("not_found", "x"))


def _job(state: str, revision: int) -> dict[str, object]:
    return {
        "correlation_id": CORR,
        "outcome": "accepted",
        "job_id": JOB,
        "course_id": COURSE,
        "state": state,
        "attempt_number": 1,
        "revision": revision,
    }


def _err(code: str, message: str) -> dict[str, object]:
    return {"code": code, "message": message, "retryable": False, "correlation_id": CORR}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    f = Fake()
    monkeypatch.setattr(
        werkzeuge,
        "_client",
        lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(f.handle)),
    )
    yield f


def _create() -> dict[str, object]:
    return werkzeuge.kurs_erzeugen(
        COURSE,
        "Sicherheit",
        "Instandhalter",
        "advanced",
        "de-DE",
        ["Risiken bewerten"],
        [SOURCE],
        3,
    )


def test_kurs_erzeugen_maps_to_job_creation_with_stable_key(fake: Fake) -> None:
    first = _create()
    again = _create()
    assert first == {"ok": True, "daten": {"job_id": JOB, "zustand": "queued", "revision": 1}}
    assert again == first
    r0, r1 = fake.requests
    assert r0.headers["Idempotency-Key"] == r1.headers["Idempotency-Key"]
    assert r0.headers["X-VibeMind-Actor-ID"] == "learning-claw"
    assert r0.headers["X-VibeMind-Space-ID"] == "learning"
    assert r0.headers["X-VibeMind-Roles"] == "learning.author"
    assert r0.headers["Authorization"] == f"Bearer {KEY}"
    assert r0.headers["X-VibeMind-Correlation-ID"] != r1.headers["X-VibeMind-Correlation-ID"]
    assert r0.url.path == "/api/v1/course-factory/jobs"
    body = json.loads(r0.content)
    assert body["expected_course_revision"] == 3
    assert body["level"] == "advanced" and body["source_ids"] == [SOURCE]


def test_idempotency_key_is_deterministic_and_content_bound() -> None:
    a = idempotency_key("t", {"x": 1, "y": 2})
    assert a == idempotency_key("t", {"y": 2, "x": 1})
    assert a.startswith("claw-") and len(a) == 45
    assert a != idempotency_key("t", {"x": 1, "y": 3})


def test_material_importieren_pfad_and_url(fake: Fake) -> None:
    out = werkzeuge.material_importieren(COURSE, "Handbuch", 3, pfad="a/b.pdf")
    assert out == {
        "ok": True,
        "daten": {"source_id": SOURCE, "revision": 1, "verarbeitung": "pending"},
    }
    body = json.loads(fake.requests[0].content)
    assert body["source"] == {"kind": "local_import", "relative_path": "a/b.pdf"}
    assert body["expected_course_revision"] == 3
    assert fake.requests[0].headers["X-VibeMind-Roles"] == "learning.author"
    werkzeuge.material_importieren(COURSE, "Web", 3, url="https://example.org/x")
    assert json.loads(fake.requests[1].content)["source"] == {
        "kind": "https",
        "url": "https://example.org/x",
    }


@pytest.mark.parametrize("kw", [{}, {"pfad": "a", "url": "https://e.org"}])
def test_material_importieren_requires_exactly_one_of_pfad_or_url(
    fake: Fake, kw: dict[str, str]
) -> None:
    out = werkzeuge.material_importieren(COURSE, "t", 0, **kw)
    assert out["ok"] is False and out["fehler"]
    assert fake.requests == []


def test_status_reads(fake: Fake) -> None:
    assert werkzeuge.kurs_status(JOB)["daten"]["zustand"] == "review_pending"
    assert werkzeuge.material_status(SOURCE)["daten"] == {
        "source_id": SOURCE,
        "kurs_id": COURSE,
        "revision": 2,
        "verarbeitung": "complete",
    }
    assert werkzeuge.kurs_lesen(COURSE)["daten"] == {
        "kurs_id": COURSE,
        "revision": 3,
        "kurs": {"title": "Sicherheit"},
    }
    assert all(r.method == "GET" for r in fake.requests)
    assert "Idempotency-Key" not in fake.requests[0].headers


def test_entwurf_ablehnen_sends_reject_with_reviewer_role(fake: Fake) -> None:
    out = werkzeuge.entwurf_ablehnen(JOB, 4, "zu oberflaechlich")
    assert out == {"ok": True, "daten": {"job_id": JOB, "zustand": "rejected", "revision": 5}}
    req = fake.requests[0]
    assert json.loads(req.content) == {
        "expected_revision": 4,
        "decision": "reject",
        "reason": "zu oberflaechlich",
    }
    assert req.headers["X-VibeMind-Roles"] == "learning.reviewer"
    assert req.headers["Idempotency-Key"].startswith("claw-")


def test_unavailable_api_is_fail_soft(fake: Fake) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake.override = down
    out = werkzeuge.kurs_status(JOB)
    assert out["ok"] is False and "nicht erreichbar" in out["fehler"]


def test_5xx_is_unavailable(fake: Fake) -> None:
    fake.override = lambda r: httpx.Response(503, text="boom")
    assert "nicht erreichbar" in werkzeuge.kurs_status(JOB)["fehler"]


def test_errors_never_contain_the_service_key(fake: Fake) -> None:
    fake.override = lambda r: httpx.Response(
        422, json=_err("weird", f"echo {r.headers['Authorization']} {KEY}")
    )
    out = werkzeuge.kurs_status(JOB)
    assert out["ok"] is False and KEY not in json.dumps(out)


def test_long_default_message_is_bounded(fake: Fake) -> None:
    fake.override = lambda r: httpx.Response(422, json=_err("weird", "m" * 500))
    assert len(werkzeuge.kurs_status(JOB)["fehler"]) <= 300


@pytest.mark.parametrize(
    ("status", "code", "text"),
    [
        (422, "source_not_ready", "Quelle ist noch nicht verarbeitet"),
        (409, "revision_conflict", "Der Stand hat sich geaendert, bitte neu laden"),
        (404, "not_found", "Nicht gefunden"),
        (409, "idempotency_conflict", "Gleiche Anfrage mit anderem Inhalt"),
        (422, "other", "api says"),
    ],
)
def test_rejection_codes_become_german_reasons(
    fake: Fake, status: int, code: str, text: str
) -> None:
    fake.override = lambda r: httpx.Response(status, json=_err(code, "api says"))
    assert _create() == {"ok": False, "fehler": text}


def test_invalid_uuid_is_fail_soft(fake: Fake) -> None:
    out = werkzeuge.kurs_status("not-a-uuid")
    assert out["ok"] is False and fake.requests == []


def test_no_tool_reaches_learnhouse_or_penecho(fake: Fake) -> None:
    _create()
    werkzeuge.material_importieren(COURSE, "t", 0, pfad="a")
    werkzeuge.material_status(SOURCE)
    werkzeuge.kurs_status(JOB)
    werkzeuge.kurs_lesen(COURSE)
    werkzeuge.entwurf_ablehnen(JOB, 1, "x")
    assert len(fake.requests) == 6
    assert all(r.url.port in {8090, 8091} for r in fake.requests)


def test_client_built_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_CLAW_API_URL", "http://h:1/api/v1")
    monkeypatch.setenv("LEARNING_CLAW_MCP_URL", "http://h:2/mcp")
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", KEY)
    c = werkzeuge._client()
    assert (c.api_url, c.mcp_url) == ("http://h:1/api/v1", "http://h:2/mcp")


def _mcp_client(handler: Callable[[httpx.Request], httpx.Response]) -> LearningClient:
    return LearningClient(API, MCP, KEY, transport=httpx.MockTransport(handler))


def _mcp_reply(payload: dict[str, object], *, is_error: bool) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": is_error,
            },
        },
    )


def test_mcp_call_envelope_and_result() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _mcp_reply({"state": "completed"}, is_error=False)

    out = _mcp_client(handler).mcp_call("learning_course_open", {"a": 1})
    assert out == {"state": "completed"}
    body = json.loads(seen[0].content)
    assert body["jsonrpc"] == "2.0" and body["method"] == "tools/call"
    assert body["params"] == {"name": "learning_course_open", "arguments": {"a": 1}}
    assert str(seen[0].url) == MCP


def test_mcp_call_is_error_raises_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _mcp_reply(
            {"state": "rejected", "error": {"code": "revision_conflict", "message": "m"}},
            is_error=True,
        )

    with pytest.raises(LearningRejected) as info:
        _mcp_client(handler).mcp_call("t", {})
    assert info.value.code == "revision_conflict"


def test_mcp_call_string_error_code() -> None:
    with pytest.raises(LearningRejected) as info:
        _mcp_client(lambda r: _mcp_reply({"error": "unknown_tool"}, is_error=True)).mcp_call(
            "t", {}
        )
    assert info.value.code == "unknown_tool"


def test_module_exposes_logger_not_print() -> None:
    assert learning_client.logger.name == "spaces.learning.claw.learning_client"


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


def test_kurs_erzeugen_reuses_the_key_for_600_seconds_then_mints_a_new_one(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(werkzeuge, "_fenster", werkzeuge.Idempotenzfenster(clock=clock))
    _create()
    clock.now += 599
    _create()
    clock.now += 2  # 601 s after the first call
    _create()
    _create()
    keys = [r.headers["Idempotency-Key"] for r in fake.requests]
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]
    assert keys[3] == keys[2]
    assert all(k.startswith("claw-") for k in keys)


def test_kurs_erzeugen_window_is_per_arguments(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(werkzeuge, "_fenster", werkzeuge.Idempotenzfenster(clock=_Clock()))
    _create()
    werkzeuge.kurs_erzeugen(COURSE, "Anderer Titel", "a", "b", "de-DE", ["x"], [SOURCE], 3)
    assert fake.requests[0].headers["Idempotency-Key"] != fake.requests[1].headers["Idempotency-Key"]


def test_deterministic_keys_stay_for_reject_and_import(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(werkzeuge, "_fenster", werkzeuge.Idempotenzfenster(clock=clock))
    werkzeuge.entwurf_ablehnen(JOB, 4, "grund")
    werkzeuge.material_importieren(COURSE, "t", 3, pfad="a")
    clock.now += 10_000
    werkzeuge.entwurf_ablehnen(JOB, 4, "grund")
    werkzeuge.material_importieren(COURSE, "t", 3, pfad="a")
    k = [r.headers["Idempotency-Key"] for r in fake.requests]
    assert k[0] == k[2] and k[1] == k[3]


def test_entwurf_ablehnen_key_uses_the_normalized_job_id(fake: Fake) -> None:
    werkzeuge.entwurf_ablehnen(JOB, 4, "grund")
    werkzeuge.entwurf_ablehnen(JOB.replace("-", ""), 4, "grund")
    k = [r.headers["Idempotency-Key"] for r in fake.requests]
    assert k[0] == k[1]


def test_each_call_closes_its_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[LearningClient] = []

    def factory() -> LearningClient:
        client = LearningClient(
            API, MCP, KEY, transport=httpx.MockTransport(lambda r: httpx.Response(503))
        )
        made.append(client)
        return client

    monkeypatch.setattr(werkzeuge, "_client", factory)
    werkzeuge.kurs_status(JOB)
    werkzeuge.naechste_aufgabe(SOURCE)
    assert len(made) == 2
    assert all(c._http.is_closed for c in made)


def test_status_says_why_the_quality_gate_holds_the_draft(fake: Fake) -> None:
    # Live a draft sat in quality_gate with no visible reason (draft_job_mismatch).
    def held(request: httpx.Request) -> httpx.Response:
        job = _job("quality_gate", 7)
        job.update(
            {
                "unsupported_claim_ids": ["claim-intro-1"],
                "quality_issue_codes": ["claim_missing_citation", "unsupported_claim"],
            }
        )
        return httpx.Response(200, json=job)

    fake.override = held
    daten = werkzeuge.kurs_status(JOB)["daten"]
    assert daten["zustand"] == "quality_gate"
    assert daten["unbelegte_aussagen"] == ["claim-intro-1"]
    assert daten["pruefung_haelt_an"] == ["claim_missing_citation", "unsupported_claim"]


def test_job_for_a_published_course_says_why(fake: Fake) -> None:
    fake.override = lambda request: httpx.Response(
        422, json=_err("course_published", "the course is published and immutable")
    )
    out = _create()
    assert out["ok"] is False and "schon veroeffentlicht" in out["fehler"]
