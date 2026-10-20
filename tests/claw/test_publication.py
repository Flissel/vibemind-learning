"""Publication: the model only displays; the person publishes on the local approval page.

There is no model tool that publishes. `veroeffentlichung_anzeigen` returns a one-time
link; pressing the button on that page (`werkzeuge.freigeben`) publishes once.
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
            "draft_summary": {
                "title": "Sicherheit im Betrieb",
                "chapters": [
                    {"title": "Vor der Arbeit", "lessons": ["Sichtpruefung", "Akku"], "activity_count": 3},
                    {"title": "Laden", "lessons": ["Ladegeraet"], "activity_count": 2},
                ],
            },
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
                    # Real shape (live): the projection nests the LearnHouse course.
                    "course": {"course": {"title": "Sicherheit"}},
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


def _display(job_id: str = JOB) -> dict[str, object]:
    out = werkzeuge.veroeffentlichung_anzeigen(job_id)
    assert out["ok"] is True, out
    return out["daten"]


OPENED: list[str] = []


@pytest.fixture(autouse=True)
def browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    OPENED.clear()
    monkeypatch.setattr(werkzeuge, "_browser_oeffnen", lambda url: OPENED.append(url) or True)
    yield OPENED


def _secret(daten: dict[str, object]) -> str:
    # The secret never reaches the model: only the browser on this machine gets the link.
    assert "freigabe_link" not in daten
    assert not any(part in json.dumps(daten) for part in ("/freigabe/", OPENED[-1].rsplit("/", 1)[1]))
    link = OPENED[-1]
    assert link.startswith("http://127.0.0.1:8139/freigabe/")
    return link.rsplit("/", 1)[1]


def test_display_shows_outline_revisions_and_a_local_link(fake: Fake) -> None:
    daten = _display()
    assert daten["titel"] == "Sicherheit im Betrieb"
    assert daten["kapitel"] == [
        {"titel": "Vor der Arbeit", "lektionen": ["Sichtpruefung", "Akku"], "aufgaben": 3},
        {"titel": "Laden", "lektionen": ["Ladegeraet"], "aufgaben": 2},
    ]
    assert daten["lektionen"] == 3
    assert daten["unbelegte_aussagen"] == ["claim-1"]
    assert daten["revision"] == 7 and daten["kurs_revision"] == 3
    assert len(_secret(daten)) >= 24
    assert fake.publish_calls == 0


def test_display_falls_back_to_the_course_title_without_an_outline(fake: Fake) -> None:
    original = fake._job

    def without_outline(job_id: str, state: str) -> dict[str, object]:
        job = original(job_id, state)
        job.pop("draft_summary")
        return job

    fake._job = without_outline  # type: ignore[method-assign]
    daten = _display()
    assert daten["titel"] == "Sicherheit"
    assert daten["kapitel"] is None and daten["lektionen"] is None


def test_the_model_has_no_publish_tool() -> None:
    from spaces.learning.claw import server

    names = {f.__name__ for f in server.WERKZEUGE}
    assert "veroeffentlichen" not in names and "freigeben" not in names


def test_pressing_the_button_publishes_once(fake: Fake) -> None:
    secret = _secret(_display())

    first = werkzeuge.freigeben(secret)
    again = werkzeuge.freigeben(secret)

    assert first["ok"] is True and first["daten"]["zustand"] == "published"
    assert again["ok"] is False and "nicht oder sie ist abgelaufen" in again["fehler"]
    assert fake.publish_calls == 1
    assert fake.last_json["confirmed"] is True and fake.last_json["expected_revision"] == 7
    assert SAFE_TOKEN.match(str(fake.last_json["approval_ref"]))


def test_unknown_secret_is_refused(fake: Fake) -> None:
    out = werkzeuge.freigeben("x" * 32)
    assert out["ok"] is False and fake.publish_calls == 0


def test_expired_approval_is_refused(fake: Fake, clock: Clock) -> None:
    secret = _secret(_display())
    clock.advance(601)
    assert werkzeuge.freigeben(secret)["ok"] is False
    assert fake.publish_calls == 0


def test_changed_job_revision_is_refused(fake: Fake) -> None:
    secret = _secret(_display())
    fake.revision = 8
    out = werkzeuge.freigeben(secret)
    assert out["ok"] is False and "geaendert" in out["fehler"]
    assert fake.publish_calls == 0


def test_changed_course_revision_is_refused(fake: Fake) -> None:
    secret = _secret(_display())
    fake.course_revision = 4
    out = werkzeuge.freigeben(secret)
    assert out["ok"] is False and "geaendert" in out["fehler"]
    assert fake.publish_calls == 0


def test_job_that_left_review_ready_is_refused(fake: Fake) -> None:
    secret = _secret(_display())
    fake.state = "published"
    assert werkzeuge.freigeben(secret)["ok"] is False
    assert fake.publish_calls == 0


def test_display_refuses_jobs_that_are_not_review_ready(fake: Fake) -> None:
    fake.state = "verifying"
    out = werkzeuge.veroeffentlichung_anzeigen(JOB)
    assert out["ok"] is False


def test_unavailable_publish_uses_the_approval_up(fake: Fake) -> None:
    secret = _secret(_display())
    fake.publish_status = 503
    assert werkzeuge.freigeben(secret)["ok"] is False
    fake.publish_status = 200
    assert werkzeuge.freigeben(secret)["ok"] is False
    assert fake.publish_calls == 1


def test_display_tells_the_model_only_that_the_page_was_opened(fake: Fake) -> None:
    daten = _display()
    assert daten["freigabe"] == "Freigabe-Seite im Browser dieses Rechners geoeffnet"
    assert len(OPENED) == 1


def test_display_says_so_when_no_browser_could_be_opened(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(werkzeuge, "_browser_oeffnen", lambda url: False)
    daten = _display()
    assert "nicht geoeffnet" in daten["freigabe"]
