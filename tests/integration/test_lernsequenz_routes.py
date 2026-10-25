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
