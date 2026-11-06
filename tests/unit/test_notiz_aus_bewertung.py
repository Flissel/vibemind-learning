"""Notiz aus der Bewertung: nur bei falscher Antwort, einmal je Aufgabe, Antwort bleibt unberuehrt."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.contracts.events import LearningToolName
from spaces.learning.contracts.mcp_models import ToolRequestV1
from spaces.learning.deployment import notizen_routes, runtime_api
from spaces.learning.mcp.tools.sessions import AdaptiveSessionGateway
from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ItemConcept,
)
from spaces.learning.services.adaptive_engine.session_service import (
    AdaptiveSessionService,
    CriterionFeedback,
    PublicTask,
    SessionStartCommand,
    TurnFeedback,
)
from spaces.learning.services.db.models import Base
from spaces.learning.services.notizen.aus_bewertung import FESTER_TEXT, notiz_aus_bewertung
from spaces.learning.services.notizen.store import NoteStore

ACTOR = "lh-1-test"


def _task(prompt: str = "Frage?") -> PublicTask:
    return PublicTask(
        id=str(uuid4()), type="single_choice", prompt=prompt, options=(), difficulty=0.3,
        representation="quiz", source_refs=(), presentation={},
    )


def _feedback(state: str, criteria: tuple[CriterionFeedback, ...] = (), refs: tuple[str, ...] = ()) -> TurnFeedback:
    return TurnFeedback(
        state=state, score=0.0, rationale_codes=(), source_refs=refs, criteria=criteria
    )


def _krit(score: float, text: str) -> CriterionFeedback:
    return CriterionFeedback(criterion_id=text or "k", description="d", score=score, feedback=text)


@pytest.mark.parametrize("state", ["correct", "needs_review"])
def test_keine_notiz_bei_richtig_oder_in_pruefung(state: str) -> None:
    assert notiz_aus_bewertung(_task(), _feedback(state), "s1") is None


def test_notiz_nimmt_schwaechstes_kriterium() -> None:
    kriterien = (_krit(0.5, "A"), _krit(0.0, "B"), _krit(0.0, ""))
    notiz = notiz_aus_bewertung(_task(), _feedback("partial", kriterien, ("src://1",)), "s1")
    assert notiz is not None
    assert notiz["explanation"] == "B"
    assert notiz["sequence_id"] == "s1" and notiz["quelle"] == "bewertung"
    assert notiz["extra"] == "" and notiz["extra_sources"] == ["src://1"]


def test_notiz_ohne_kriterien_hat_festen_text() -> None:
    task = _task()
    notiz = notiz_aus_bewertung(task, _feedback("incorrect"), "s1")
    assert notiz is not None
    assert notiz["explanation"] == FESTER_TEXT
    assert FESTER_TEXT == "Diese Antwort war noch nicht richtig. Sehen Sie sich die Aufgabe noch einmal in Ruhe an."
    assert notiz["anchor"] == {"art": "aufgabe", "aufgabe": task.id}


def test_notiz_auswahl_ist_klartext_und_gekuerzt() -> None:
    notiz = notiz_aus_bewertung(_task("$x^2$ ableiten"), _feedback("incorrect"), "s1")
    assert notiz is not None and "$" not in notiz["selection"] and "x²" in notiz["selection"]
    lang = notiz_aus_bewertung(_task("a" * 3000), _feedback("incorrect"), "s1")
    assert lang is not None and len(lang["selection"]) == 2000


def _factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'n.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_store_trennt_quellen(tmp_path: Path) -> None:
    store = NoteStore(_factory(tmp_path))
    basis = dict(
        actor_id=ACTOR, course_id="c1", sequence_id="s1", anchor={"section_id": "f1"},
        selection="x", explanation="y", extra="", extra_sources=[],
    )
    alt = store.create(**basis)
    neu = store.create(**{**basis, "anchor": {"art": "aufgabe", "aufgabe": "t"}}, quelle="bewertung")
    assert alt["quelle"] == "sequenz" and neu["quelle"] == "bewertung"
    assert [n["id"] for n in store.list_for(ACTOR)] == [alt["id"]]
    assert [n["id"] for n in store.list_for(ACTOR, quelle="bewertung")] == [neu["id"]]
    with pytest.raises(ValueError):
        store.create(**basis, quelle="anders")


def _gateway_welt(tmp_path: Path, notes_factory=None):
    engine = create_engine(f"sqlite:///{tmp_path / 'g.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    course_id = str(uuid4())
    with factory() as db, db.begin():
        for key in ("alpha", "beta"):
            concept_id, item_id = str(uuid4()), str(uuid4())
            db.add(AdaptiveConcept(id=concept_id, course_id=course_id, course_revision=1, concept_key=key, title=key))
            db.add(AdaptiveItem(
                id=item_id, course_id=course_id, course_revision=1, activity_id=f"a-{key}",
                item_type="single_choice", difficulty=0.3, quality_status="approved",
                expected_answer={"choice_id": "a"},
                scoring_config={
                    "prompt": "Frage?", "options": [{"id": "a", "label": "Ja"}, {"id": "b", "label": "Nein"}],
                    "source_refs": ["source://course/1"], "hints": ["Hinweis."],
                    "representation": "quiz", "remediation_tags": [],
                },
                rubric=[],
            ))
            db.add(ItemConcept(item_id=item_id, concept_id=concept_id, course_id=course_id, course_revision=1))
    service = AdaptiveSessionService(factory)
    store = NoteStore(factory)
    return engine, service, store, course_id


def _antwort(session_id: str, revision: int, choice: str) -> ToolRequestV1:
    return ToolRequestV1.model_validate({
        "tool": LearningToolName.TASK_ANSWER.value,
        "event": {
            "event_type": "learning.task.answer", "invocation_id": str(uuid4()),
            "correlation_id": str(uuid4()), "idempotency_key": str(uuid4()),
            "actor": {"actor_id": ACTOR, "actor_type": "local_user"},
            "session_id": session_id, "expected_revision": revision,
            "payload": {"answer": {"choice_id": choice}},
        },
    })


def _start(service: AdaptiveSessionService, course_id: str, mode: str = "training"):
    return service.start(SessionStartCommand(
        course_id=course_id, course_revision=1, actor_id=ACTOR, mode=mode,
        blueprint={"max_attempts": 5} if mode == "training" else {"item_count": 2},
    ))


def test_gateway_legt_bei_falscher_antwort_einmal_notiz_an(tmp_path: Path) -> None:
    engine, service, store, course_id = _gateway_welt(tmp_path)
    gateway = AdaptiveSessionGateway(service, store)
    turn = _start(service, course_id)
    erste = turn.task
    outcome = gateway.execute(_antwort(turn.session_id, turn.session_revision, "b"))
    assert outcome.state == "completed"
    notizen = store.list_for(ACTOR, quelle="bewertung")
    assert len(notizen) == 1
    assert notizen[0]["anchor"] == {"art": "aufgabe", "aufgabe": erste.id}
    assert notizen[0]["sequence_id"] == turn.session_id and notizen[0]["course_id"] == course_id
    assert store.list_for(ACTOR) == []

    # Dieselbe (Sitzung, Aufgabe) nochmal bewertet: weiterhin eine Notiz.
    wieder = service.current(turn.session_id, actor_id=ACTOR).model_copy(
        update={"feedback": _feedback("incorrect")}
    )
    gateway._note_from_feedback(wieder, erste, ACTOR)  # noqa: SLF001
    assert len(store.list_for(ACTOR, quelle="bewertung")) == 1
    engine.dispose()


def test_gateway_richtig_und_pruefungsmodus_ohne_notiz(tmp_path: Path) -> None:
    engine, service, store, course_id = _gateway_welt(tmp_path)
    gateway = AdaptiveSessionGateway(service, store)
    turn = _start(service, course_id)
    gateway.execute(_antwort(turn.session_id, turn.session_revision, "a"))
    pruefung = _start(service, course_id, mode="exam")
    gateway.execute(_antwort(pruefung.session_id, pruefung.session_revision, "b"))
    assert store.list_for(ACTOR, quelle="bewertung") == []
    engine.dispose()


def test_gateway_antwort_bleibt_bei_notizfehler(tmp_path: Path) -> None:
    engine, service, store, course_id = _gateway_welt(tmp_path)

    class Kaputt:
        def list_for(self, *args, **kwargs):
            raise RuntimeError("boom")

        create = list_for

    turn = _start(service, course_id)
    outcome = AdaptiveSessionGateway(service, Kaputt()).execute(  # type: ignore[arg-type]
        _antwort(turn.session_id, turn.session_revision, "b")
    )
    assert outcome.state == "completed"
    assert outcome.result["session"]["feedback"]["state"] == "incorrect"
    engine.dispose()


KEY = "shared-with-learnhouse-" + "x" * 16
KOPF = {"X-LearnHouse-Learning-Service-Key": KEY, "X-Learning-Actor": ACTOR}


def test_route_filtert_quelle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LEARNING_DATABASE_URL", f"sqlite:///{tmp_path / 'r.db'}")
    monkeypatch.setenv("LEARNHOUSE_LEARNING_SERVICE_KEY", KEY)
    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(engine)
    engine.dispose()
    notizen_routes._store.cache_clear()
    try:
        client = TestClient(runtime_api.app, raise_server_exceptions=False)
        store = notizen_routes._store()
        basis = dict(
            actor_id=ACTOR, course_id="c1", sequence_id="s1", anchor={"a": 1},
            selection="x", explanation="y", extra="", extra_sources=[],
        )
        alt = store.create(**basis)
        neu = store.create(**basis, quelle="bewertung")
        ohne = client.get("/api/v1/notizen", headers=KOPF).json()["notes"]
        assert [n["id"] for n in ohne] == [alt["id"]]
        mit = client.get("/api/v1/notizen?quelle=bewertung", headers=KOPF).json()["notes"]
        assert [n["id"] for n in mit] == [neu["id"]] and mit[0]["quelle"] == "bewertung"
        assert client.get("/api/v1/notizen?quelle=zzz", headers=KOPF).status_code == 400
    finally:
        notizen_routes._store.cache_clear()


def test_migration_0019_head(tmp_path: Path) -> None:
    from spaces.learning.deployment.migrate import migrate
    from spaces.learning.services.db.session import create_learning_engine

    url = f"sqlite:///{tmp_path / 'm.db'}"
    migrate(url)
    engine = create_learning_engine(url)
    try:
        with engine.begin() as c:
            assert c.exec_driver_sql("SELECT version_num FROM learning_alembic_version").scalar_one() == "0020_vorbewertungen"
            c.exec_driver_sql(
                "INSERT INTO learning_notes (id, actor_id, course_id, sequence_id, anchor, selection, explanation,"
                " extra, extra_sources, own_text, created_at, updated_at)"
                " VALUES ('n1','a','c','s','{}','x','y','','[]','','2026-01-01','2026-01-01')"
            )
            assert c.exec_driver_sql("SELECT quelle FROM learning_notes WHERE id='n1'").scalar_one() == "sequenz"
    finally:
        engine.dispose()

# ---- Zeichenabgaben: derselbe Haken wie bei Antworten (Abschlusspruefung, Befund 10) -----

class _Bewertung:
    """Stellvertreter fuer CanvasRubricResultV1 (nur die Felder, die CanvasGateway liest)."""

    def __init__(self) -> None:
        self.evaluation_id = uuid4()

    def model_dump(self, *, mode: str) -> dict:
        return {"evaluation_id": str(self.evaluation_id)}


def _zeichen_gateway(service, notes, session_id: str, revision: int):
    from types import SimpleNamespace
    from uuid import UUID

    from spaces.learning.mcp.tools.canvas import CanvasGateway, CanvasSubmissionOutcome
    from spaces.learning.services.adaptive_engine.session_service import AnswerCommand

    leinwand = SimpleNamespace(
        id=UUID(session_id), revision=3,
        launch=SimpleNamespace(actor_id=ACTOR, source_refs=()),
    )

    class PenEcho:
        def open_session(self, sid, ref):
            return leinwand

        def export_session(self, sid, ref):
            return SimpleNamespace(snapshot_png_base64="iVBORw0KGgo=")

    class Artefakte:
        def import_export(self, export):
            return SimpleNamespace()

    class Bewertungen:
        # Wie learning-worker: die Abgabe wird als Antwort der adaptiven Sitzung bewertet (hier falsch).
        def submit(self, **werte):
            turn = service.answer(AnswerCommand(
                session_id=werte["session_id"], actor_id=werte["actor_id"],
                expected_session_revision=werte["expected_session_revision"], answer={"choice_id": "b"},
            ))
            return CanvasSubmissionOutcome(result=_Bewertung(), turn=turn)  # type: ignore[arg-type]

    gateway = CanvasGateway(penecho=PenEcho(), artifacts=Artefakte(), evaluations=Bewertungen(),
                            tutor=object(), notes=notes)  # type: ignore[arg-type]
    request = ToolRequestV1.model_validate({
        "tool": LearningToolName.CANVAS_SUBMIT.value,
        "event": {
            "event_type": "learning.canvas.submit", "invocation_id": str(uuid4()),
            "correlation_id": str(uuid4()), "idempotency_key": str(uuid4()),
            "actor": {"actor_id": ACTOR, "actor_type": "local_user"},
            "session_id": session_id, "expected_revision": 3,
            "confirmation": {"confirmed": True, "approval_ref": "test-1"},
            "payload": {"session_ref": "ref-1", "submission_id": str(uuid4()),
                        "adaptive_session_revision": revision},
        },
    })
    return gateway, request


def test_falsche_zeichnung_legt_notiz_an(tmp_path: Path) -> None:
    from spaces.learning.mcp.tools.sessions import BewertungsNotizen

    engine, service, store, course_id = _gateway_welt(tmp_path)
    turn = _start(service, course_id)
    erste = turn.task
    haken = BewertungsNotizen(service, store)
    gateway, request = _zeichen_gateway(service, haken, turn.session_id, turn.session_revision)
    outcome = gateway.execute(request)
    assert outcome.state == "completed", outcome
    notizen = store.list_for(ACTOR, quelle="bewertung")
    assert len(notizen) == 1
    assert notizen[0]["anchor"] == {"art": "aufgabe", "aufgabe": erste.id}
    assert notizen[0]["sequence_id"] == turn.session_id and notizen[0]["course_id"] == course_id
    # Gleiche Entdoppelung wie bei Antworten: dieselbe (Sitzung, Aufgabe) bleibt eine Notiz.
    wieder = service.current(turn.session_id, actor_id=ACTOR).model_copy(update={"feedback": _feedback("incorrect")})
    haken.nachher(wieder, erste, ACTOR)
    AdaptiveSessionGateway(service, store)._note_from_feedback(wieder, erste, ACTOR)  # noqa: SLF001
    assert len(store.list_for(ACTOR, quelle="bewertung")) == 1
    engine.dispose()


def test_zeichnung_ohne_notizen_bleibt_wie_bisher(tmp_path: Path) -> None:
    engine, service, store, course_id = _gateway_welt(tmp_path)
    turn = _start(service, course_id)
    gateway, request = _zeichen_gateway(service, None, turn.session_id, turn.session_revision)
    assert gateway.execute(request).state == "completed"
    assert store.list_for(ACTOR, quelle="bewertung") == []
    engine.dispose()


def test_server_verdrahtet_notizen_fuer_zeichnungen() -> None:
    import inspect

    from spaces.learning.mcp import server

    quelle = inspect.getsource(server)
    assert quelle.count("notes=bewertungs_notizen") == 2
