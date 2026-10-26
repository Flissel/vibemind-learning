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
