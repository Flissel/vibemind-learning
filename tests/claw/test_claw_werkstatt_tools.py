"""Werkstatt tools of the Learning claw against a faked Learning API."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from spaces.learning.claw import server, werkzeuge
from spaces.learning.claw.learning_client import LearningClient

KEY = "k" * 40
API = "http://127.0.0.1:8090/api/v1"
MCP = "http://127.0.0.1:8091/mcp"
BASE = "/api/v1/werkstatt"
TASK = "task-1"
COURSE = "kurs-1"
CORR = "44444444-4444-4444-8444-444444444444"


class Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            200, json={"ok_marker": True}
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self) -> Any:
        return json.loads(self.last.content)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    f = Fake()
    monkeypatch.setattr(
        werkzeuge,
        "_client",
        lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(f.handle)),
    )
    yield f


def _check(fake: Fake, method: str, path: str, *, write: bool) -> None:
    request = fake.last
    assert request.method == method
    assert request.url.path == BASE + path
    assert request.headers["X-VibeMind-Roles"] == "learning.author"
    if write:
        assert request.headers["X-Werkstatt-Author"] == "claw"


def test_kurs_neu(fake: Fake) -> None:
    fake.answer = lambda r: httpx.Response(202, json={"auftrag_id": "a1"})
    result = werkzeuge.werkstatt_kurs_neu(
        COURSE, "Titel", "Einsteiger", ["Ziel"], ["s1"], standard_typ="open", hinweise=["kurz"]
    )
    assert result == {"ok": True, "daten": {"auftrag_id": "a1"}}
    _check(fake, "POST", "/kurse", write=True)
    assert fake.body() == {
        "course_id": COURSE, "title": "Titel", "audience": "Einsteiger", "outcomes": ["Ziel"],
        "source_ids": ["s1"], "default_kind": "open", "hinweise": ["kurz"],
    }


def test_kurs_neu_omits_unset_options(fake: Fake) -> None:
    werkzeuge.werkstatt_kurs_neu(COURSE, "T", "Z", ["x"], [])
    assert "default_kind" not in fake.body() and "hinweise" not in fake.body()


def test_kurs_zeigen(fake: Fake) -> None:
    assert werkzeuge.werkstatt_kurs_zeigen(COURSE)["ok"] is True
    _check(fake, "GET", f"/kurse/{COURSE}", write=False)


def test_kurs_aendern(fake: Fake) -> None:
    werkzeuge.werkstatt_kurs_aendern(COURSE, titel="Neu", standard_typ="quiz", reihenfolge=["a", "b"])
    _check(fake, "PATCH", f"/kurse/{COURSE}", write=True)
    assert fake.body() == {"title": "Neu", "default_kind": "quiz", "reihenfolge": ["a", "b"]}


def test_kurs_aendern_without_change_is_refused(fake: Fake) -> None:
    result = werkzeuge.werkstatt_kurs_aendern(COURSE)
    assert result["ok"] is False and not fake.requests


def test_kurs_uebernehmen(fake: Fake) -> None:
    werkzeuge.werkstatt_kurs_uebernehmen(COURSE)
    _check(fake, "POST", f"/kurse/{COURSE}/uebernehmen", write=True)


def test_grobentwurf_neu(fake: Fake) -> None:
    fake.answer = lambda r: httpx.Response(202, json={"auftrag_id": "a3"})
    result = werkzeuge.werkstatt_grobentwurf_neu(COURSE, hinweise=["kuerzer"])
    assert result == {"ok": True, "daten": {"auftrag_id": "a3"}}
    _check(fake, "POST", f"/kurse/{COURSE}/grobentwurf", write=True)
    assert fake.body() == {"hinweise": ["kuerzer"]}


def test_grobentwurf_neu_ohne_hinweise(fake: Fake) -> None:
    werkzeuge.werkstatt_grobentwurf_neu(COURSE)
    _check(fake, "POST", f"/kurse/{COURSE}/grobentwurf", write=True)
    assert fake.body() == {}


def test_aufgabe_neu(fake: Fake) -> None:
    werkzeuge.werkstatt_aufgabe_neu(COURSE, "Kapitel 1", "Kurz", position=2)
    _check(fake, "POST", f"/kurse/{COURSE}/aufgaben", write=True)
    assert fake.body() == {"chapter_title": "Kapitel 1", "summary": "Kurz", "position": 2}


def test_aufgabe_ausformulieren(fake: Fake) -> None:
    fake.answer = lambda r: httpx.Response(202, json={"auftrag_id": "a2"})
    result = werkzeuge.werkstatt_aufgabe_ausformulieren(TASK, None, wunsch="leichter", typ="cloze")
    assert result["daten"] == {"auftrag_id": "a2"}
    _check(fake, "POST", f"/aufgaben/{TASK}/ausformulieren", write=True)
    assert fake.body() == {"based_on": None, "wunsch": "leichter", "kind": "cloze"}


def test_aufgabe_aendern(fake: Fake) -> None:
    werkzeuge.werkstatt_aufgabe_aendern(TASK, 3, {"prompt": "x"}, "Tippfehler")
    _check(fake, "PATCH", f"/aufgaben/{TASK}", write=True)
    assert fake.body() == {"based_on": 3, "felder": {"prompt": "x"}, "reason": "Tippfehler"}


def test_aufgabe_loeschen(fake: Fake) -> None:
    werkzeuge.werkstatt_aufgabe_loeschen(TASK)
    _check(fake, "DELETE", f"/aufgaben/{TASK}", write=True)


def test_fassungen(fake: Fake) -> None:
    werkzeuge.werkstatt_fassungen(TASK)
    _check(fake, "GET", f"/aufgaben/{TASK}/fassungen", write=False)


def test_zurueckholen(fake: Fake) -> None:
    werkzeuge.werkstatt_zurueckholen(TASK, 2)
    _check(fake, "POST", f"/aufgaben/{TASK}/zurueckholen", write=True)
    assert fake.body() == {"number": 2}


def test_vorschau_with_and_without_fassung(fake: Fake) -> None:
    werkzeuge.werkstatt_vorschau(TASK, fassung=4)
    _check(fake, "GET", f"/aufgaben/{TASK}/vorschau", write=False)
    assert fake.last.url.params["fassung"] == "4"
    werkzeuge.werkstatt_vorschau(TASK)
    assert "fassung" not in fake.last.url.params


def test_auftrag(fake: Fake) -> None:
    werkzeuge.werkstatt_auftrag("a1")
    _check(fake, "GET", "/auftraege/a1", write=False)


def test_ids_cannot_escape_the_path(fake: Fake) -> None:
    werkzeuge.werkstatt_kurs_zeigen("../x?y")
    assert fake.last.url.raw_path.decode().startswith(BASE + "/kurse/..%2Fx%3Fy")


def test_kein_werkstatt_werkzeug_veroeffentlicht(fake: Fake) -> None:
    # The release display has its own tests (test_claw_werkstatt_freigabe.py): it opens the
    # approval page and releases nothing itself.
    names = [
        f.__name__ for f in server.WERKZEUGE
        if f.__name__.startswith("werkstatt_") and f is not werkzeuge.werkstatt_freigabe_anzeigen
    ]
    assert len(names) == 13
    assert not any("veroeffentlich" in n or "freigeb" in n or "publish" in n for n in names)
    calls: dict[str, tuple[Any, ...]] = {
        "werkstatt_kurs_neu": (COURSE, "T", "Z", ["x"], []),
        "werkstatt_kurs_zeigen": (COURSE,),
        "werkstatt_kurs_aendern": (COURSE, "T"),
        "werkstatt_kurs_uebernehmen": (COURSE,),
        "werkstatt_grobentwurf_neu": (COURSE,),
        "werkstatt_aufgabe_neu": (COURSE, "K", "S"),
        "werkstatt_aufgabe_ausformulieren": (TASK, 1),
        "werkstatt_aufgabe_aendern": (TASK, 1, {}, "g"),
        "werkstatt_aufgabe_loeschen": (TASK,),
        "werkstatt_fassungen": (TASK,),
        "werkstatt_zurueckholen": (TASK, 1),
        "werkstatt_vorschau": (TASK,),
        "werkstatt_auftrag": ("a1",),
    }
    assert set(calls) == set(names)
    for name, args in calls.items():
        getattr(werkzeuge, name)(*args)
    assert fake.requests
    assert not any("publish" in str(r.url) or "veroeffentlich" in str(r.url) for r in fake.requests)


def test_konflikt_kommt_als_fehler_mit_neuester_zurueck(fake: Fake) -> None:
    neueste = {"number": 5, "content": {"prompt": "aktuell"}}
    fake.answer = lambda r: httpx.Response(
        409,
        json={"code": "konflikt", "message": "Die Aufgabe wurde inzwischen geaendert",
              "retryable": False, "correlation_id": CORR, "neueste": neueste},
    )
    result = werkzeuge.werkstatt_aufgabe_aendern(TASK, 3, {"prompt": "x"}, "g")
    assert result["ok"] is False
    assert "geaendert" in result["fehler"]
    assert result["neueste"] == neueste
