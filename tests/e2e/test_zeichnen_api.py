"""Drawing tasks in the LearnHouse player: the link comes from canvas open, learning-api's
``/zeichnen/<token>`` page acts only as the person of the link, through the real MCP
handling (handle_message) into the session and canvas gateways.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from spaces.learning.bridge.dispatcher import InMemoryReceiptStore, LearningDispatcher
from spaces.learning.bridge.local_canvas import LocalCanvasBackend, LocalCanvasLauncher
from spaces.learning.deployment import runtime_api, zeichnen_routes
from spaces.learning.mcp import server as mcp_server
from spaces.learning.mcp.tools.canvas import (
    CanvasAdaptiveSubmissionService, CanvasGateway, build_canvas_gateways,
)
from spaces.learning.mcp.tools.sessions import build_session_gateways
from spaces.learning.services.adaptive_engine.models import AdaptiveConcept, AdaptiveItem, ItemConcept
from spaces.learning.services.adaptive_engine.repository import SessionInput
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService
from spaces.learning.services.db.models import Base
from spaces.learning.services.evaluation.canvas_artifacts import CanvasArtifactStore
from spaces.learning.services.zeichnen.link import DrawingGrant, drawing_token, drawing_url, read_drawing_token
from spaces.learning.services.werkstatt.freigabe import loesung_hash
from spaces.learning.services.werkstatt.katalog import WerkstattKatalog
from spaces.learning.tests.e2e.test_local_canvas import REF, _Gateway, _Tutor

KEY = "z" * 32
PUBLIC = "http://100.67.177.45:8090"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nzeichnung").decode()
WERKSTATT_PROMPT = "Skizzieren Sie $f(x)=x^2$ und markieren Sie den Scheitel."
STRICHE = [{"punkte": [[10, 10], [200, 90]], "farbe": "#111111", "breite": 5}]


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", KEY)
    monkeypatch.setenv("LEARNING_PUBLIC_API_URL", PUBLIC)
    engine = create_engine(f"sqlite:///{tmp_path / 'zeichnen.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, connection_record) -> None:
        del connection_record
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    course_id, concept_id, item = uuid4(), uuid4(), UUID(int=7)
    with factory() as db, db.begin():
        db.add(AdaptiveConcept(id=str(concept_id), course_id=str(course_id), course_revision=1,
                               concept_key="sichern", title="Sichern"))
        db.add(AdaptiveItem(
            id=str(item), course_id=str(course_id), course_revision=1, activity_id="k4-zeichnung",
            item_type="penecho_canvas", difficulty=0.5, quality_status="approved",
            expected_answer={"text": "Werkstueck in Zwinge."},
            scoring_config={"prompt": "Zeichne das gesicherte Werkstueck.", "options": [],
                            "source_refs": [REF], "hints": ["Denk an die Zwinge."], "presentation": {}},
            rubric=[{"criterion_id": "zwinge", "description": "Zwinge sichtbar", "points": 1}],
        ))
        db.add(ItemConcept(item_id=str(item), concept_id=str(concept_id), course_id=str(course_id),
                           course_revision=1))
    # A task from the Werkstatt (sketch), released into its own course the way a release does it.
    werkstatt_course = str(uuid4())
    werkstatt_inhalt = {
        "kind": "sketch", "prompt": WERKSTATT_PROMPT, "concept_ids": ["k1"],
        "expected_answer": "Nach oben offene Parabel.",
        "rubric": [
            {"criterion_id": "r1", "description": "Parabel nach oben offen", "points": 2},
            {"criterion_id": "r2", "description": "Scheitel im Ursprung", "points": 2},
            {"criterion_id": "r3", "description": "Achsen beschriftet", "points": 1},
        ],
    }
    WerkstattKatalog(factory).projizieren(werkstatt_course, 1, [{
        "task_id": "t1", "fassung": 1, "aufgaben_key": "skizze-1",
        "loesung_hash": loesung_hash(werkstatt_inhalt), "content": werkstatt_inhalt,
    }], None, fach="mathe")
    with factory() as db, db.begin():
        row = db.scalars(select(AdaptiveItem).where(AdaptiveItem.course_id == werkstatt_course)).one()
        werkstatt_item = UUID(row.id)
        # A cited task (the check demands a citation for sketches); the projection of citations
        # into source_refs is the catalog's own, tested in test_werkstatt_katalog.
        row.scoring_config = {**row.scoring_config, "source_refs": [REF]}
    sessions = AdaptiveSessionService(factory, external_rubric_types=frozenset({"penecho_canvas"}))
    werkstatt_started = sessions.start(SessionInput(
        course_id=werkstatt_course, course_revision=1, actor_id="lh-3-lernende", mode="training",
        blueprint={"max_attempts": 1},
    ))
    started = sessions.start(SessionInput(course_id=str(course_id), course_revision=1, actor_id="lh-3-lernende",
                                          mode="training", blueprint={"max_attempts": 1}))
    backend = LocalCanvasBackend(root=tmp_path / "canvas", origin="http://127.0.0.1:8139")
    (tmp_path / "artifacts").mkdir()
    canvas = CanvasGateway(
        penecho=backend,
        artifacts=CanvasArtifactStore(artifact_root=tmp_path / "artifacts"),
        evaluations=CanvasAdaptiveSubmissionService(session_factory=factory, gateway=_Gateway()),
        tutor=_Tutor(),
        launcher=LocalCanvasLauncher(session_factory=factory, backend=backend),
        page_link=drawing_url,
    )
    dispatcher = LearningDispatcher(
        gateways={**build_session_gateways(sessions), **build_canvas_gateways(canvas)},
        receipts=InMemoryReceiptStore(),
    )

    def post(url, *, json, timeout):  # noqa: A002 - httpx signature
        del url, timeout
        return SimpleNamespace(json=lambda: mcp_server.handle_message(json, dispatcher=dispatcher))

    monkeypatch.setattr(zeichnen_routes.httpx, "post", post)
    client = TestClient(runtime_api.app, raise_server_exceptions=False)
    client.werkstatt = (werkstatt_started, werkstatt_item)  # type: ignore[attr-defined]
    yield client, started, item, post
    engine.dispose()


def _open(post, started, item, actor="lh-3-lernende") -> dict:
    answer = post("", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "learning_canvas_open", "arguments": {
            "event_type": "learning.canvas.open", "invocation_id": str(uuid4()),
            "correlation_id": str(uuid4()), "actor": {"actor_id": actor, "actor_type": "local_user"},
            "session_id": started.session_id,
            "payload": {"activity_id": str(item), "session_ref": "lh-z-1"},
        }}}, timeout=1).json()
    return {**json.loads(answer["result"]["content"][0]["text"]), "is_error": answer["result"]["isError"]}


def test_token_is_bound_signed_and_expires() -> None:
    grant = DrawingGrant(session_id=str(uuid4()), activity_id=str(uuid4()), session_ref="r", actor_id="lh-1-a")
    token = drawing_token(grant, service_key=KEY, now=1000)
    assert read_drawing_token(token, service_key=KEY, now=1001) == grant
    with pytest.raises(LookupError):
        read_drawing_token(token, service_key="y" * 32, now=1001)
    with pytest.raises(LookupError):
        read_drawing_token(token[:-1] + ("0" if token[-1] != "0" else "1"), service_key=KEY, now=1001)
    with pytest.raises(LookupError):
        read_drawing_token(token, service_key=KEY, now=1000 + 13 * 3600)


def test_the_person_draws_gets_a_hint_and_submits_in_the_player(world) -> None:
    client, started, item, post = world
    opened = _open(post, started, item)
    assert opened["is_error"] is False
    url = opened["result"]["page_url"]
    assert url.startswith(PUBLIC + "/zeichnen/")
    path = url.removeprefix(PUBLIC)

    page = client.get(path)
    assert page.status_code == 200 and "Zeichne das gesicherte Werkstueck." in page.text
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]

    assert client.post(path, json={"png": PNG, "striche": STRICHE}).status_code == 403  # no Origin
    origin = {"origin": PUBLIC}
    hint = client.post(path + "/hinweis", json={}, headers=origin)
    assert hint.status_code == 200 and hint.json()["hinweis"]

    submitted = client.post(path, json={"png": PNG, "striche": STRICHE}, headers=origin)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["daten"]["punkte"] == 1.0
    assert client.post(path, json={"png": PNG, "striche": STRICHE}, headers=origin).json()["ok"] is False


def test_a_forged_link_and_another_person_get_nothing(world) -> None:
    client, started, item, post = world
    assert client.get("/zeichnen/abc.def").status_code == 404
    assert _open(post, started, item, actor="lh-3-mallory")["is_error"] is True


def test_a_werkstatt_sketch_item_opens_the_drawing_page_like_a_factory_item(world) -> None:
    client, _started, _item, post = world
    started, item = client.werkstatt  # type: ignore[attr-defined]
    opened = _open(post, started, item)
    assert opened["is_error"] is False, opened
    url = opened["result"]["page_url"]
    assert url.startswith(PUBLIC + "/zeichnen/")  # same path as a factory item
    path = url.removeprefix(PUBLIC)

    page = client.get(path)
    assert page.status_code == 200 and "Skizzieren Sie" in page.text and "markieren Sie den Scheitel" in page.text
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
