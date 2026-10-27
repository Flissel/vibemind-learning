"""Changed sources over the service API, for the claw tool: author role required."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from spaces.learning.deployment import quellen_routes, runtime_api
from spaces.learning.services.db.models import Base
from spaces.learning.services.quellen.frische import Change

KEY = "k" * 32


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", KEY)
    monkeypatch.setenv("LEARNING_DATABASE_URL", f"sqlite:///{tmp_path / 'quellen.db'}")
    engine = create_engine(f"sqlite:///{tmp_path / 'quellen.db'}")
    Base.metadata.create_all(engine)
    engine.dispose()
    quellen_routes._store.cache_clear()
    yield TestClient(runtime_api.app, raise_server_exceptions=False)
    quellen_routes._store.cache_clear()


def _author(roles: str = "learning.author") -> dict[str, str]:
    return {"Authorization": f"Bearer {KEY}", "X-VibeMind-Actor-ID": "learning-claw", "X-VibeMind-Space-ID": "learning",
            "X-VibeMind-Roles": roles, "X-VibeMind-Correlation-ID": str(uuid4())}


def test_changed_sources_need_the_author_role(client: TestClient) -> None:
    assert client.get("/api/v1/quellen/frische").status_code == 401
    assert client.get("/api/v1/quellen/frische", headers=_author("learning.learner")).status_code == 403
    assert client.get("/api/v1/quellen/frische", headers=_author()).json() == {"changed": []}


def test_changed_sources_are_listed_per_course(client: TestClient) -> None:
    store = quellen_routes._store()
    store.take_up(course_id="c1", url="https://docs.example/install", sha256="a" * 64)
    store.take_up(course_id="c1", url="https://docs.example/faq", sha256="b" * 64)
    store.record([Change(course_id="c1", url="https://docs.example/install", old_sha="a" * 64, new_sha="d" * 64)],
                 now=datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc))

    listed = client.get("/api/v1/quellen/frische", headers=_author()).json()["changed"]
    assert [c["url"] for c in listed] == ["https://docs.example/install"]
    assert listed[0]["course_id"] == "c1" and listed[0]["changed_at"].startswith("2026-10-27")
