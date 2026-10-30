"""Kursbewertung over the service API: learners via LearnHouse, owner via the VibeMind key."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from spaces.learning.deployment import bewertungen_routes, runtime_api
from spaces.learning.services.bewertungen import models as _models  # noqa: F401  (registers the table)
from spaces.learning.services.db.models import Base
from spaces.learning.tests.integration.course_factory_api_support import SERVICE_KEY, headers

KEY = "shared-with-learnhouse-" + "x" * 16
SERVICE = "X-LearnHouse-Learning-Service-Key"
ACTOR = "X-Learning-Actor"
ANNA = {SERVICE: KEY, ACTOR: "lh-3-anna-geheim"}
BEN = {SERVICE: KEY, ACTOR: "lh-3-ben"}
KURS = str(uuid4())


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    url = f"sqlite:///{tmp_path / 'bew.db'}"
    monkeypatch.setenv("LEARNING_DATABASE_URL", url)
    monkeypatch.setenv("LEARNHOUSE_LEARNING_SERVICE_KEY", KEY)
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", SERVICE_KEY)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    bewertungen_routes._store.cache_clear()
    yield TestClient(runtime_api.app, raise_server_exceptions=False)
    bewertungen_routes._store.cache_clear()


def _abgeben(client: TestClient, hdr: dict, **felder):
    body = {"course_id": KURS, "kurs_titel": "ROI verstehen", "smiley": "gut", "stichworte": ["klar"]}
    body.update(felder)
    return client.post("/api/v1/kursbewertung", headers=hdr, json=body)


def test_abgeben_und_lesen(client: TestClient) -> None:
    antwort = _abgeben(client, ANNA)
    assert antwort.status_code == 200 and antwort.json() == {"gespeichert": True, "neu": True}
    assert _abgeben(client, ANNA, smiley="mittel").json() == {"gespeichert": True, "neu": False}
    eigene = client.get(f"/api/v1/kursbewertung/{KURS}", headers=ANNA).json()["bewertung"]
    assert eigene["smiley"] == "mittel" and eigene["stichworte"] == ["klar"] and eigene["aktualisiert"]
    assert client.get(f"/api/v1/kursbewertung/{KURS}", headers=BEN).json() == {"bewertung": None}


def test_ohne_schluessel_oder_person(client: TestClient) -> None:
    assert client.post("/api/v1/kursbewertung", json={}).status_code == 403
    assert _abgeben(client, {SERVICE: "falsch-" + "w" * 40, ACTOR: "x"}).status_code == 403
    assert _abgeben(client, {SERVICE: KEY}).status_code == 400
    assert client.get(f"/api/v1/kursbewertung/{KURS}").status_code == 403


def test_fehlercodes(client: TestClient) -> None:
    antwort = _abgeben(client, ANNA, stichworte=["a", "b", "c", "d"])
    assert antwort.status_code == 400 and antwort.json()["code"] == "zu_viele_stichworte"
    assert _abgeben(client, ANNA, stichworte=["x" * 41]).json()["code"] == "stichwort_zu_lang"
    assert _abgeben(client, ANNA, smiley="super").json()["code"] == "smiley_unbekannt"
    assert _abgeben(client, ANNA, course_id="kein-uuid").json()["code"] == "course_id_ungueltig"
    assert _abgeben(client, ANNA, stichworte="klar").status_code == 400


def test_unbekannter_kurs_wird_angenommen(client: TestClient) -> None:
    assert _abgeben(client, ANNA, course_id=str(uuid4()), kurs_titel="").status_code == 200


def test_course_prefix_wird_normalisiert(client: TestClient) -> None:
    assert _abgeben(client, ANNA, course_id=f"course_{KURS}").status_code == 200
    assert client.get(f"/api/v1/kursbewertung/{KURS}", headers=ANNA).json()["bewertung"] is not None


def test_inhaber_routen(client: TestClient) -> None:
    _abgeben(client, ANNA)
    _abgeben(client, BEN, smiley="schlecht", stichworte=["zäh"])
    for pfad in ("/api/v1/kursbewertungen/neu", "/api/v1/kursbewertungen/uebersicht"):
        assert client.get(pfad).status_code == 401
        assert client.get(pfad, headers=headers(key="falsch-" + "w" * 40)).status_code == 401
        assert client.get(pfad, headers=KEYLESS_LEARNER(KEY)).status_code == 401
    neu = client.get("/api/v1/kursbewertungen/neu", headers=headers())
    assert neu.status_code == 200 and len(neu.json()["bewertungen"]) == 2
    uebersicht = client.get("/api/v1/kursbewertungen/uebersicht", headers=headers())
    assert uebersicht.status_code == 200 and uebersicht.json()["kurse"][0]["kurs_titel"] == "ROI verstehen"
    for text in (neu.text, uebersicht.text):
        assert "anna" not in text and "lh-3" not in text and "actor" not in text

    eintraege = [{"id": b["id"], "aktualisiert": b["aktualisiert"]} for b in neu.json()["bewertungen"]]
    assert client.post("/api/v1/kursbewertungen/gemeldet", json={"eintraege": eintraege}).status_code == 401
    erledigt = client.post(
        "/api/v1/kursbewertungen/gemeldet", headers=headers(), json={"eintraege": eintraege}
    )
    assert erledigt.status_code == 200 and erledigt.json() == {"markiert": 2}
    assert client.get("/api/v1/kursbewertungen/neu", headers=headers()).json() == {"bewertungen": []}
    assert client.post("/api/v1/kursbewertungen/gemeldet", headers=headers(), json={"eintraege": "x"}).status_code == 400


def KEYLESS_LEARNER(key: str) -> dict:
    return {SERVICE: key, ACTOR: "lh-3-anna-geheim"}


def test_inhaber_routen_brauchen_autorenrolle(client: TestClient) -> None:
    ohne = headers(roles="learning.reviewer")
    assert client.get("/api/v1/kursbewertungen/neu", headers=ohne).status_code == 403
    assert client.get("/api/v1/kursbewertungen/uebersicht", headers=ohne).status_code == 403
    antwort = client.post("/api/v1/kursbewertungen/gemeldet", headers=ohne, json={"eintraege": []})
    assert antwort.status_code == 403
