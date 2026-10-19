"""Publication tools: a revision-bound display from an earlier MCP session (spec 9.1).

Each chat turn of the shim is a fresh `claude -p` with a fresh MCP session, so a display
in one session and the publish call in a later one means a person turn lay in between.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import httpx
import pytest

from spaces.learning.claw import werkzeuge
from spaces.learning.claw.freigabe import FreigabeStore
from spaces.learning.claw.learning_client import LearningClient

KEY = "k" * 40
COURSE = "11111111-1111-4111-8111-111111111111"
JOB = "33333333-3333-4333-8333-333333333333"
OTHER_JOB = "55555555-5555-4555-8555-555555555555"
API = "http://127.0.0.1:8090/api/v1"
MCP = "http://127.0.0.1:8091/mcp"
CORR = "44444444-4444-4444-8444-444444444444"
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

TURN_1 = "sitzung-turn-1"
TURN_2 = "sitzung-turn-2"


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
        self.course_revision = 3
        self.publish_requests: list[httpx.Request] = []
        self.publish_status = 200

    @property
    def publish_calls(self) -> int:
        return len(self.publish_requests)

    @property
    def last_json(self) -> dict[str, object]:
        return json.loads(self.publish_requests[-1].content)

    def _job(self, job_id: str, state: str) -> dict[str, object]:
        return {
            "correlation_id": CORR,
            "outcome": "completed",
            "job_id": job_id,
            "course_id": COURSE,
            "state": state,
            "attempt_number": 1,
            "revision": self.revision,
            "unsupported_claim_ids": ["claim-1"],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for job_id in (JOB, OTHER_JOB):
            if request.method == "GET" and path.endswith(f"/course-factory/jobs/{job_id}"):
                return httpx.Response(200, json=self._job(job_id, self.state))
        if request.method == "GET" and path.endswith(f"/courses/{COURSE}"):
            return httpx.Response(
                200,
                json={
                    "correlation_id": CORR,
                    "outcome": "completed",
                    "course_id": COURSE,
                    "revision": self.course_revision,
                    "course": {"title": "Sicherheit"},
                },
            )
        if request.method == "POST" and path.endswith("/publish"):
            self.publish_requests.append(request)
            if self.publish_status >= 500:
                return httpx.Response(self.publish_status)
            return httpx.Response(200, json=self._job(JOB, "published"))
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


def _display(session: str = TURN_1, job_id: str = JOB) -> dict[str, object]:
    out = werkzeuge.veroeffentlichung_anzeigen(job_id, mcp_sitzung=session)
    assert out["ok"] is True, out
    return out["daten"]


def _publish(
    session: str | None = TURN_2, job_id: str = JOB, revision: int = 7, kurs_revision: int = 3
) -> dict[str, object]:
    return werkzeuge.veroeffentlichen(job_id, revision, kurs_revision, mcp_sitzung=session)


def test_display_shows_both_revisions_and_no_token(fake: Fake) -> None:
    daten = _display()
    assert daten["titel"] == "Sicherheit"
    assert daten["kapitel"] is None and daten["lektionen"] is None
    assert daten["unbelegte_aussagen"] == ["claim-1"]
    assert daten["revision"] == 7 and daten["kurs_revision"] == 3
    assert "token" not in daten
    assert "freigabe-" not in json.dumps(daten)


def test_display_then_yes_in_a_later_session_publishes_once(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    clock.advance(30)
    out = _publish(TURN_2)
    assert out["ok"] is True, out
    body = fake.last_json
    assert body["expected_revision"] == 7 and body["confirmed"] is True
    ref = body["approval_ref"]
    assert isinstance(ref, str) and ref.startswith("freigabe-") and SAFE_TOKEN.match(ref)
    request = fake.publish_requests[-1]
    assert request.headers["x-vibemind-roles"] == "learning.publisher"
    assert request.headers["idempotency-key"]
    assert fake.publish_calls == 1


def test_display_and_publish_in_the_same_session_are_refused(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    out = _publish(TURN_1)
    assert out["ok"] is False and "spaeter" in out["fehler"]
    assert fake.publish_calls == 0
    # The refused attempt does not burn the display: the person's later yes still works.
    clock.advance(5)
    assert _publish(TURN_2)["ok"] is True


def test_publish_without_any_display_is_refused(fake: Fake) -> None:
    out = _publish(TURN_2)
    assert out["ok"] is False and out["fehler"]
    assert fake.publish_calls == 0


def test_expired_display_is_refused(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    clock.advance(601)
    out = _publish(TURN_2)
    assert out["ok"] is False and "abgelaufen" in out["fehler"]
    assert fake.publish_calls == 0


def test_display_is_single_use(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    clock.advance(1)
    assert _publish(TURN_2)["ok"] is True
    fake.state = "review_ready"  # even if the job were ready again
    assert _publish("sitzung-turn-3")["ok"] is False
    assert fake.publish_calls == 1


def test_changed_job_revision_is_refused(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    fake.revision = 8
    clock.advance(1)
    assert _publish(TURN_2, revision=7)["ok"] is False
    # Revision 8 was never displayed.
    assert _publish("sitzung-turn-3", revision=8)["ok"] is False
    assert fake.publish_calls == 0


def test_changed_course_revision_is_refused(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    fake.course_revision = 4
    clock.advance(1)
    out = _publish(TURN_2, kurs_revision=3)
    assert out["ok"] is False and "geaendert" in out["fehler"]
    assert _publish("sitzung-turn-3", kurs_revision=4)["ok"] is False
    assert fake.publish_calls == 0


def test_revisions_that_were_never_displayed_are_refused(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    clock.advance(1)
    assert _publish(TURN_2, revision=6)["ok"] is False
    assert _publish(TURN_2, kurs_revision=2)["ok"] is False
    assert fake.publish_calls == 0


def test_display_of_another_job_does_not_authorize(fake: Fake, clock: Clock) -> None:
    _display(TURN_1, job_id=OTHER_JOB)
    clock.advance(1)
    assert _publish(TURN_2, job_id=JOB)["ok"] is False
    assert fake.publish_calls == 0


@pytest.mark.parametrize("session", [None, ""])
def test_publish_without_session_id_is_refused(
    fake: Fake, clock: Clock, session: str | None
) -> None:
    _display(TURN_1)
    clock.advance(1)
    out = _publish(session)
    assert out["ok"] is False and "Sitzung" in out["fehler"]
    assert fake.publish_calls == 0


def test_display_without_session_id_never_authorizes(fake: Fake, clock: Clock) -> None:
    werkzeuge.veroeffentlichung_anzeigen(JOB, mcp_sitzung=None)
    clock.advance(1)
    assert _publish(TURN_2)["ok"] is False
    assert fake.publish_calls == 0


def test_display_refuses_jobs_that_are_not_review_ready(fake: Fake) -> None:
    fake.state = "queued"
    assert werkzeuge.veroeffentlichung_anzeigen(JOB, mcp_sitzung=TURN_1)["ok"] is False


def test_publish_refuses_when_job_left_review_ready(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    fake.state = "published"
    clock.advance(1)
    assert _publish(TURN_2)["ok"] is False
    assert fake.publish_calls == 0


def test_unavailable_publish_consumes_the_display(fake: Fake, clock: Clock) -> None:
    _display(TURN_1)
    clock.advance(1)
    fake.publish_status = 503
    assert _publish(TURN_2)["ok"] is False
    fake.publish_status = 200
    assert _publish("sitzung-turn-3")["ok"] is False
    assert fake.publish_calls == 1


def test_tool_signature_has_no_token(fake: Fake) -> None:
    import inspect

    params = list(inspect.signature(werkzeuge.veroeffentlichen).parameters)
    assert params[:3] == ["job_id", "revision", "kurs_revision"]
    assert "token" not in params
