"""Sequence player over HTTP: token per sequence, no solutions in page or data, server grading."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spaces.learning.contracts.lernsequenz import LernsequenzV1
from spaces.learning.deployment import lernsequenz_routes, runtime_api
from spaces.learning.services.lernsequenz.store import SequenceStore
from spaces.learning.tests.unit.test_lernsequenz import sequence_data

KEY = "k" * 32


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", KEY)
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    lernsequenz_routes._store.cache_clear()
    SequenceStore(tmp_path).save(LernsequenzV1.model_validate(sequence_data()))
    yield TestClient(runtime_api.app, raise_server_exceptions=False)
    lernsequenz_routes._store.cache_clear()


def _token() -> str:
    return lernsequenz_routes.sequence_token("seq-roi", KEY)


def test_page_and_data_need_the_sequence_token(client) -> None:
    assert client.get("/lernsequenz/seq-roi?k=" + "0" * 40).status_code == 404
    assert client.get("/lernsequenz/seq-roi/daten").status_code == 400
    page = client.get(f"/lernsequenz/seq-roi?k={_token()}")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'self' http://127.0.0.1:13000" in csp and "script-src 'nonce-" in csp
    assert "__NONCE__" not in page.text


def test_data_has_no_solutions_and_answers_are_graded(client) -> None:
    data = client.get(f"/lernsequenz/seq-roi/daten?k={_token()}").json()
    assert "correct" not in data["checkpoints"][0] and data["checkpoints"][0]["multiple"] is False
    assert "expected" not in data["checkpoints"][1]["steps"][0]

    url = f"/lernsequenz/seq-roi/antwort?k={_token()}"
    right = client.post(url, json={"checkpoint_id": "q1", "answer": {"selected": ["b"]}})
    assert right.status_code == 200 and right.json()["correct"] is True
    calc = client.post(url, json={"checkpoint_id": "roi", "answer": {"values": {"h": "1000", "eur": "45.000"}}})
    assert [s["follow_up"] for s in calc.json()["steps"]] == [False, True]
    bad = client.post(url, json={"checkpoint_id": "q1", "answer": {"selected": []}})
    assert bad.status_code == 400
    assert client.post(url, json={"checkpoint_id": "nope", "answer": {}}).status_code == 404


def test_reading_endpoint_explains_how_a_number_is_read(client) -> None:
    r = client.post(f"/lernsequenz/seq-roi/lesen?k={_token()}", json={"text": "45,000"})
    assert r.status_code == 200 and r.json()["ambiguous"] is True
    assert r.json()["display"] == "45" and r.json()["alternative"]["display"] == "45.000"
    assert client.post(f"/lernsequenz/seq-roi/lesen?k={_token()}", json={"text": "40-50"}).json()["reason"] == "spanne"
    assert client.post("/lernsequenz/seq-roi/lesen?k=" + "0" * 40, json={"text": "1"}).status_code == 404


def test_mitmach_answers_are_graded_over_http(client, tmp_path: Path) -> None:
    from spaces.learning.tests.unit.test_lernsequenz_mitmach import mitmach_data

    data = mitmach_data()
    data["sequence_id"] = "seq-mitmach"
    SequenceStore(tmp_path).save(LernsequenzV1.model_validate(data))
    token = lernsequenz_routes.sequence_token("seq-mitmach", KEY)
    url = f"/lernsequenz/seq-mitmach/antwort?k={token}"
    answers = {
        "pr": {"choices": {"shell": "cmd"}},
        "ord": {"order": ["konto", "laden", "anmelden"]},
        "cl": {"values": {"datei": "install.ps1"}},
        "mt": {"pairs": {"l1": "r1", "l2": "r2", "l3": "r3"}},
        "hs": {"rect": {"x": 290, "y": 30, "w": 100, "h": 50}},
        "ts": {"command": "claude --version"},
        "st": {"state": "done"},
    }
    for checkpoint_id, answer in answers.items():
        response = client.post(url, json={"checkpoint_id": checkpoint_id, "answer": answer})
        assert response.status_code == 200, (checkpoint_id, response.text)
        assert checkpoint_id == "pr" or response.json()["score"] == 1.0
    duplicate = client.post(url, json={"checkpoint_id": "ord", "answer": {"order": ["konto", "konto", "laden"]}})
    assert duplicate.status_code == 400
    view = client.get(f"/lernsequenz/seq-mitmach/daten?k={token}").json()
    assert all("correct" not in c for c in view["checkpoints"] if c["kind"] == "ordering")


def test_drawings_are_saved_for_a_person(client, tmp_path: Path) -> None:
    url = f"/lernsequenz/seq-roi/antwort?k={_token()}"
    saved = client.post(url, json={"checkpoint_id": "tafel", "answer": {"strokes": [[[1, 2], [5, 6]]]}})
    assert saved.status_code == 200 and saved.json()["saved"] is True
    assert list((tmp_path / "lernsequenzen" / "skizzen" / "seq-roi" / "tafel").glob("*.json"))


def test_media_only_by_plain_name(client, tmp_path: Path) -> None:
    media = tmp_path / "lernsequenzen" / "medien"
    media.mkdir(parents=True)
    (media / "intro.mp4").write_bytes(b"\x00\x00video")
    assert client.get("/lernsequenz-medien/intro.mp4").status_code == 200
    assert client.get("/lernsequenz-medien/..%2Fsecret.mp4").status_code == 404
    assert client.get("/lernsequenz-medien/intro.exe").status_code == 404


def _author() -> dict[str, str]:
    from uuid import uuid4

    return {"Authorization": f"Bearer {KEY}", "X-VibeMind-Actor-ID": "laura-video", "X-VibeMind-Space-ID": "learning",
            "X-VibeMind-Roles": "learning.author", "X-VibeMind-Correlation-ID": str(uuid4())}


def test_media_upload_and_video_registration_need_the_author(client, tmp_path: Path) -> None:
    assert client.put("/api/v1/lernsequenz-medien/x.mp4", content=b"video").status_code == 401
    assert client.put("/api/v1/lernsequenz-medien/../x.mp4", content=b"v", headers=_author()).status_code in (400, 404)
    uploaded = client.put("/api/v1/lernsequenz-medien/laura-roi.mp4", content=b"video-bytes", headers=_author())
    assert uploaded.status_code == 200 and uploaded.json()["bytes"] == 11
    assert client.get("/lernsequenz-medien/laura-roi.mp4").content == b"video-bytes"
    assert not list((tmp_path / "lernsequenzen" / "medien").glob(".*.part"))

    missing = client.post("/api/v1/lernsequenzen/seq-roi/video", headers=_author(),
                          json={"media_name": "fehlt.mp4", "slide_times": {}})
    assert missing.status_code == 404


# --- a marked place: explain it, or keep it as a note -------------------------------

PERSON_KEY = "shared-with-learnhouse-" + "x" * 16


def _fake_worker(monkeypatch) -> list[dict]:
    import httpx

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(json.loads(request.content))
        assert request.url.path == "/internal/sequence/erklaeren"
        return httpx.Response(200, json={"explanation": {
            "explanation": "Die Jahreskosten laut Folie.", "source_keys": ["S1"],
            "extra": "Allgemein: Kosten je Jahr sind Stückkosten mal Menge.", "extra_is_outside_course": True,
            "evidence_ref": "openfang://completion/e1",
        }})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), timeout=kw.get("timeout")))
    monkeypatch.setenv("LEARNING_EVALUATION_SERVICE_URL", "http://worker")
    monkeypatch.setenv("LEARNING_EVALUATION_SERVICE_KEY", "e" * 32)
    return calls


def test_a_marked_place_is_explained_from_the_course(client, monkeypatch) -> None:
    calls = _fake_worker(monkeypatch)
    url = f"/lernsequenz/seq-roi/erklaeren?k={_token()}"
    explained = client.post(url, json={"anchor": {"section_id": "f1"}, "selection": "45.000 €"})
    assert explained.status_code == 200, explained.text
    body = explained.json()
    assert body["explanation"] == "Die Jahreskosten laut Folie." and body["extra_is_outside_course"] is True
    assert body["cited"] and body["extra"].startswith("Allgemein")
    assert calls[0]["task"]["selection"] == "45.000 €" and calls[0]["sources"][0]["key"] == "S1"
    assert client.post(url, json={"anchor": {"section_id": "f1"}}).status_code == 400
    assert client.post(url, json={"anchor": {"section_id": "nope"}, "selection": "x"}).status_code == 404


def test_a_note_needs_the_person_and_keeps_the_explanation(client, monkeypatch, tmp_path: Path) -> None:
    from sqlalchemy import create_engine

    from spaces.learning.deployment import notizen_routes
    from spaces.learning.services.db.models import Base
    from spaces.learning.services.lernsequenz.person import person_token

    _fake_worker(monkeypatch)
    monkeypatch.setenv("LEARNING_DATABASE_URL", f"sqlite:///{tmp_path / 'notizen.db'}")
    monkeypatch.setenv("LEARNHOUSE_LEARNING_SERVICE_KEY", PERSON_KEY)
    engine = create_engine(f"sqlite:///{tmp_path / 'notizen.db'}")
    Base.metadata.create_all(engine)
    engine.dispose()
    notizen_routes._store.cache_clear()
    url = f"/lernsequenz/seq-roi/notizen?k={_token()}"
    body = {"anchor": {"checkpoint_id": "q1"}, "selection": "ROI"}

    anonymous = client.post(url, json=body)
    saved = client.post(url, json=body, headers={"X-Learning-Person": person_token("lh-3-anna", key=PERSON_KEY)})
    listed = client.get("/api/v1/notizen", headers={"X-LearnHouse-Learning-Service-Key": PERSON_KEY, "X-Learning-Actor": "lh-3-anna"})
    notizen_routes._store.cache_clear()

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "person_required"
    assert saved.status_code == 200 and saved.json()["note_id"]
    note = listed.json()["notes"][0]
    assert note["id"] == saved.json()["note_id"] and note["selection"] == "ROI"
    assert note["course_id"] == "b189a3ec-a82a-4ae2-8cab-e71ab0d75055" and note["anchor"] == {"checkpoint_id": "q1"}
    assert note["explanation"] == "Die Jahreskosten laut Folie." and note["extra"].startswith("Allgemein")
