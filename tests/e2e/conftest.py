"""The sequence player in a real browser against a learning-api started in this process."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PLAYER_KEY = "p" * 32


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def player_server(tmp_path: Path, monkeypatch):
    """learning-api with a temporary artifact root; yields base url, store and token maker."""
    uvicorn = pytest.importorskip("uvicorn")
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", PLAYER_KEY)
    monkeypatch.setenv("LEARNHOUSE_LEARNING_SERVICE_KEY", PLAYER_KEY)   # signs the person token
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEARNING_DATABASE_URL", f"sqlite:///{tmp_path / 'player.db'}")   # notes, mastery
    from sqlalchemy import create_engine

    from spaces.learning.deployment import lernsequenz_routes, notizen_routes, runtime_api
    from spaces.learning.services.db.models import Base
    from spaces.learning.services.lernsequenz.store import SequenceStore
    from spaces.learning.services.lernsequenz.token import sequence_token

    engine = create_engine(f"sqlite:///{tmp_path / 'player.db'}")
    Base.metadata.create_all(engine)
    engine.dispose()
    lernsequenz_routes._store.cache_clear()
    lernsequenz_routes._sessions.cache_clear()
    notizen_routes._store.cache_clear()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(runtime_api.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("learning-api did not start")
    yield SimpleNamespace(
        base=f"http://127.0.0.1:{port}",
        store=SequenceStore(tmp_path),
        link=lambda sequence_id: f"http://127.0.0.1:{port}/lernsequenz/{sequence_id}?k={sequence_token(sequence_id, PLAYER_KEY)}",
    )
    server.should_exit = True
    thread.join(timeout=5)
    lernsequenz_routes._store.cache_clear()
    lernsequenz_routes._sessions.cache_clear()
    notizen_routes._store.cache_clear()


@pytest.fixture()
def explain_worker(monkeypatch):
    """A stand-in for learning-worker's explain endpoint; records what it was asked."""
    uvicorn = pytest.importorskip("uvicorn")
    from fastapi import Body, FastAPI

    app = FastAPI()
    calls: list[dict] = []

    @app.post("/internal/sequence/erklaeren")
    def explain(payload: dict = Body()) -> dict:
        calls.append(payload)
        return {"explanation": {
            "explanation": "Die Jahreskosten laut Folie.", "source_keys": ["S1"],
            "extra": "Allgemein: Kosten je Jahr sind Stückkosten mal Menge.", "extra_is_outside_course": True,
            "extra_sources": [], "evidence_ref": "openfang://completion/e1",
        }}

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    monkeypatch.setenv("LEARNING_EVALUATION_SERVICE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("LEARNING_EVALUATION_SERVICE_KEY", "e" * 32)
    yield SimpleNamespace(calls=calls)
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def browser_page():
    """A Chromium page; the player's strict CSP blocks Playwright's evaluate, so it is bypassed."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as error:  # noqa: BLE001 - no browser binaries installed
            pytest.skip(f"chromium not available: {error}")
        page = browser.new_context(viewport={"width": 1280, "height": 960}, bypass_csp=True).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.errors = errors  # type: ignore[attr-defined]
        yield page
        browser.close()
