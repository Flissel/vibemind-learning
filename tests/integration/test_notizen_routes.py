"""Notes over the service API (LearnHouse proxies for the signed-in person): service key
and actor header required, answers never carry the actor id."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from spaces.learning.deployment import notizen_routes, runtime_api
from spaces.learning.services.db.models import Base

KEY = "shared-with-learnhouse-" + "x" * 16
SERVICE = "X-LearnHouse-Learning-Service-Key"
ACTOR = "X-Learning-Actor"
LERNENDE = {SERVICE: KEY, ACTOR: "lh-3-lernende"}
BEN = {SERVICE: KEY, ACTOR: "lh-3-ben"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LEARNING_DATABASE_URL", f"sqlite:///{tmp_path / 'notizen.db'}")
    monkeypatch.setenv("LEARNHOUSE_LEARNING_SERVICE_KEY", KEY)
    engine = create_engine(f"sqlite:///{tmp_path / 'notizen.db'}")
    Base.metadata.create_all(engine)
    engine.dispose()
    notizen_routes._store.cache_clear()
    yield TestClient(runtime_api.app, raise_server_exceptions=False)
    notizen_routes._store.cache_clear()


def _note(actor_id: str = "lh-3-lernende") -> dict:
    return notizen_routes._store().create(
        actor_id=actor_id, course_id="c1", sequence_id="seq-roi", anchor={"section_id": "f1"},
        selection="45.000 €", explanation="Jährliche Kosten laut Folie.", extra="", extra_sources=[],
    )


def test_notes_need_the_service_key_and_an_actor(client: TestClient) -> None:
    assert client.get("/api/v1/notizen").status_code == 403
    assert client.get("/api/v1/notizen", headers={SERVICE: "wrong-key-" + "w" * 30, ACTOR: "lh-3-lernende"}).status_code == 403
    assert client.get("/api/v1/notizen", headers={SERVICE: KEY}).status_code == 400
    assert client.get("/api/v1/notizen", headers=LERNENDE).json() == {"notes": []}


def test_listing_and_own_text_are_per_person(client: TestClient) -> None:
    note = _note()
    listed = client.get("/api/v1/notizen", headers=LERNENDE).json()["notes"]
    assert [n["id"] for n in listed] == [note["id"]] and "actor_id" not in listed[0]
    assert client.get("/api/v1/notizen", headers=BEN).json() == {"notes": []}

    changed = client.patch(f"/api/v1/notizen/{note['id']}", headers=LERNENDE, json={"own_text": "Merken!"})
    assert changed.status_code == 200 and changed.json()["own_text"] == "Merken!"
    assert client.patch(f"/api/v1/notizen/{note['id']}", headers=BEN, json={"own_text": "fremd"}).status_code == 404
    assert client.patch(f"/api/v1/notizen/{note['id']}", headers=LERNENDE, json={"own_text": "x" * 10_001}).status_code == 400
    assert client.patch(f"/api/v1/notizen/{note['id']}", headers=LERNENDE, json={}).status_code == 400
