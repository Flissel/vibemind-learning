"""Publication tools: a fresh display token is required (spec 9.1)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from spaces.learning.claw import werkzeuge
from spaces.learning.claw.freigabe import FreigabeStore
from spaces.learning.claw.learning_client import LearningClient

KEY = "k" * 40
COURSE = "11111111-1111-4111-8111-111111111111"
JOB = "33333333-3333-4333-8333-333333333333"
API = "http://127.0.0.1:8090/api/v1"
MCP = "http://127.0.0.1:8091/mcp"
CORR = "44444444-4444-4444-8444-444444444444"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Fake:
    def __init__(self) -> None:
        self.state = "review_ready"
        self.revision = 7
        self.publish_requests: list[httpx.Request] = []
        self.publish_status = 200

    @property
    def publish_calls(self) -> int:
        return len(self.publish_requests)

    @property
    def last_json(self) -> dict[str, object]:
        return json.loads(self.publish_requests[-1].content)

    def bump_revision(self) -> None:
        self.revision += 1

    def _job(self, state: str) -> dict[str, object]:
        return {
            "correlation_id": CORR,
            "outcome": "completed",
            "job_id": JOB,
            "course_id": COURSE,
            "state": state,
            "attempt_number": 1,
            "revision": self.revision,
            "unsupported_claim_ids": ["claim-1"],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith(f"/course-factory/jobs/{JOB}"):
            return httpx.Response(200, json=self._job(self.state))
        if request.method == "GET" and path.endswith(f"/courses/{COURSE}"):
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
        if request.method == "POST" and path.endswith(f"/course-factory/jobs/{JOB}/publish"):
            self.publish_requests.append(request)
            if self.publish_status >= 500:
                return httpx.Response(self.publish_status)
            return httpx.Response(200, json=self._job("published"))
        return httpx.Response(404, json={"code": "not_found", "message": "x"})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Iterator[Fake]:
    f = Fake()
    monkeypatch.setattr(
        werkzeuge,
        "_client",
        lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(f.handle)),
    )
    monkeypatch.setattr(werkzeuge, "_freigaben", FreigabeStore(clock=clock))
    yield f


def test_display_returns_summary_and_token(fake: Fake) -> None:
    out = werkzeuge.veroeffentlichung_anzeigen(JOB)
    assert out["ok"] and out["daten"]["token"].startswith("freigabe-")
    assert {"titel", "kapitel", "lektionen", "unbelegte_aussagen", "revision"} <= set(out["daten"])
    assert out["daten"]["titel"] == "Sicherheit"
    assert out["daten"]["kapitel"] is None and out["daten"]["lektionen"] is None
    assert out["daten"]["unbelegte_aussagen"] == ["claim-1"]
    assert out["daten"]["revision"] == 7


def test_publish_uses_token_as_approval_ref_once(fake: Fake) -> None:
    token = werkzeuge.veroeffentlichung_anzeigen(JOB)["daten"]["token"]
    assert werkzeuge.veroeffentlichen(JOB, token)["ok"] is True
    assert fake.last_json == {"expected_revision": 7, "confirmed": True, "approval_ref": token}
    request = fake.publish_requests[-1]
    assert request.headers["x-vibemind-roles"] == "learning.publisher"
    assert request.headers["idempotency-key"]
    assert werkzeuge.veroeffentlichen(JOB, token)["ok"] is False
    assert fake.publish_calls == 1


def test_publish_refuses_unknown_expired_and_stale_tokens(fake: Fake, clock: Clock) -> None:
    assert werkzeuge.veroeffentlichen(JOB, "freigabe-0000000000000000")["ok"] is False
    token = werkzeuge.veroeffentlichung_anzeigen(JOB)["daten"]["token"]
    clock.advance(601)
    assert werkzeuge.veroeffentlichen(JOB, token)["ok"] is False
    token = werkzeuge.veroeffentlichung_anzeigen(JOB)["daten"]["token"]
    fake.bump_revision()
    assert werkzeuge.veroeffentlichen(JOB, token)["ok"] is False
    assert fake.publish_calls == 0


def test_display_refuses_jobs_that_are_not_review_ready(fake: Fake) -> None:
    fake.state = "queued"
    assert werkzeuge.veroeffentlichung_anzeigen(JOB)["ok"] is False


def test_publish_refuses_when_job_left_review_ready(fake: Fake) -> None:
    token = werkzeuge.veroeffentlichung_anzeigen(JOB)["daten"]["token"]
    fake.state = "published"
    assert werkzeuge.veroeffentlichen(JOB, token)["ok"] is False
    assert fake.publish_calls == 0


def test_unavailable_publish_consumes_token(fake: Fake) -> None:
    token = werkzeuge.veroeffentlichung_anzeigen(JOB)["daten"]["token"]
    fake.publish_status = 503
    assert werkzeuge.veroeffentlichen(JOB, token)["ok"] is False
    fake.publish_status = 200
    assert werkzeuge.veroeffentlichen(JOB, token)["ok"] is False
    assert fake.publish_calls == 1


def test_token_is_bound_to_its_job(fake: Fake) -> None:
    store = werkzeuge._freigaben
    token = store.ausstellen("other-job", 7)
    assert werkzeuge.veroeffentlichen(JOB, token)["ok"] is False
    assert fake.publish_calls == 0
