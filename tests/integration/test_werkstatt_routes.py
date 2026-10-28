"""Werkstatt HTTP API: courses, tasks and versions over the real app and a SQLite database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from spaces.learning.deployment import runtime_api, werkstatt_routes
from spaces.learning.deployment.vibemind_auth import SERVICE_KEY_ENV
from spaces.learning.services.db.models import LearningArtifact
from spaces.learning.services.ingestion.models import (
    LearningSource,
    LearningSourceChunk,
    LearningSourceRevision,
)

from spaces.learning.tests.integration.course_factory_api_support import SERVICE_KEY, ApiHarness, headers, sha256

AUTOR = {"X-Werkstatt-Author": "anna"}
BEN = {"X-Werkstatt-Author": "ben"}
TEXT = "Der Befehl Test-Path prueft, ob eine Datei oder ein Ordner existiert."
BEFEHL = "Test-Path C:/Daten/bericht.txt"


def quiz(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "quiz",
        "prompt": "Welcher Befehl prueft, ob eine Datei existiert?",
        "concept_ids": ["k1"],
        "options": [
            {"option_id": "a", "text": "Test-Path"},
            {"option_id": "b", "text": "Get-Date"},
            {"option_id": "c", "text": "Get-Help"},
        ],
        "correct_option_ids": ["a"],
    }
    base.update(over)
    return base


class Env:
    def __init__(self, harness: ApiHarness, client: TestClient) -> None:
        self.harness = harness
        self.client = client
        self.course_id = harness.course_id

    def h(self, extra: dict[str, str] | None = AUTOR, roles: str | None = None) -> dict[str, str]:
        value = headers(roles) if roles is not None else headers()
        value.update(extra or {})
        return value

    def kurs(self) -> dict[str, Any]:
        response = self.client.post(
            "/api/v1/werkstatt/kurse",
            headers=self.h(),
            json={
                "course_id": self.course_id,
                "title": "PowerShell Grundlagen",
                "audience": "Einsteiger",
                "outcomes": ["Dateien pruefen"],
                "source_ids": [self.harness.ready_source],
            },
        )
        assert response.status_code == 202, response.text
        return response.json()

    def aufgabe(self, summary: str = "Dateien pruefen", chapter: str = "Kapitel 1") -> str:
        response = self.client.post(
            f"/api/v1/werkstatt/kurse/{self.course_id}/aufgaben",
            headers=self.h(),
            json={"chapter_title": chapter, "summary": summary},
        )
        assert response.status_code == 201, response.text
        return response.json()["task_id"]

    def patch(self, task_id: str, based_on: int | None, felder: dict[str, Any], *,
              reason: str = "Aenderung", who: dict[str, str] = AUTOR):
        return self.client.patch(
            f"/api/v1/werkstatt/aufgaben/{task_id}",
            headers=self.h(who),
            json={"based_on": based_on, "felder": felder, "reason": reason},
        )

    def erste_fassung(self, task_id: str) -> dict[str, Any]:
        response = self.patch(task_id, None, quiz())
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[Env]:
    harness = ApiHarness(tmp_path)
    monkeypatch.setenv("LEARNING_DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv(SERVICE_KEY_ENV, SERVICE_KEY)
    chunk_text = f"Mit {BEFEHL} pruefen Sie, ob eine Datei existiert."
    with harness.session_factory() as session, session.begin():
        session.add(LearningSourceChunk(
            id=str(uuid4()), source_id=harness.ready_source, source_revision=1, ordinal=0,
            content=chunk_text, content_hash=sha256(chunk_text.encode()), locator={"page": 1},
            locator_hash=sha256(b"page1"), metadata_json={},
        ))
    werkstatt_routes._runtime.cache_clear()
    yield Env(harness, TestClient(runtime_api.app, raise_server_exceptions=False))
    werkstatt_routes._runtime.cache_clear()
    runtime_api.app.dependency_overrides.clear()


def test_kurs_anlegen_startet_grobentwurf(env: Env) -> None:
    auftrag_id = env.kurs()["auftrag_id"]
    auftrag = env.client.get(f"/api/v1/werkstatt/auftraege/{auftrag_id}", headers=env.h()).json()
    assert auftrag["state"] == "queued"
    kurs = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h()).json()
    assert kurs["title"] == "PowerShell Grundlagen"
    assert [p["source_id"] for p in kurs["source_provenance"]] == [env.harness.ready_source]
    assert set(kurs["source_provenance"][0]) == {"source_id", "revision", "content_hash"}
    assert kurs["aufgaben"] == []
    store = werkstatt_routes._runtime().store
    assert store.auftrag(auftrag_id)["kind"] == "grobentwurf"


def test_kurs_mit_nicht_bereiter_quelle_ist_409(env: Env) -> None:
    offen = env.harness.add_source(status="pending")
    response = env.client.post(
        "/api/v1/werkstatt/kurse",
        headers=env.h(),
        json={"course_id": env.course_id, "title": "T", "audience": "A", "outcomes": ["x"],
              "source_ids": [offen]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "quellen_nicht_bereit"


def test_kurs_doppelt_ist_409(env: Env) -> None:
    env.kurs()
    response = env.client.post(
        "/api/v1/werkstatt/kurse",
        headers=env.h(),
        json={"course_id": env.course_id, "title": "T", "audience": "A", "outcomes": ["x"],
              "source_ids": [env.harness.ready_source]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "kurs_existiert"


def test_unbekannter_kurs_ist_404(env: Env) -> None:
    response = env.client.get("/api/v1/werkstatt/kurse/gibt-es-nicht", headers=env.h())
    assert response.status_code == 404
    assert response.json()["code"] == "kurs_unbekannt"


def test_kurs_zeigt_aufgaben_mit_fassung_und_befunden(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    leer = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h()).json()
    assert leer["aufgaben"][0]["neueste_fassung"] is None
    assert leer["aufgaben"][0]["befunde"] == 0
    env.erste_fassung(task_id)
    eintrag = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h()).json()["aufgaben"][0]
    assert eintrag["summary"] == "Dateien pruefen"
    assert eintrag["neueste_fassung"] == 1


def test_kurs_aendern_titel_art_und_reihenfolge(env: Env) -> None:
    env.kurs()
    a, b = env.aufgabe("Eins"), env.aufgabe("Zwei")
    response = env.client.patch(
        f"/api/v1/werkstatt/kurse/{env.course_id}",
        headers=env.h(),
        json={"title": "Neuer Titel", "default_kind": "open", "reihenfolge": [b, a]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["title"], body["default_kind"]) == ("Neuer Titel", "open")
    assert [x["task_id"] for x in body["aufgaben"]] == [b, a]


def test_kurs_aendern_unvollstaendige_reihenfolge_ist_422(env: Env) -> None:
    env.kurs()
    a = env.aufgabe("Eins")
    env.aufgabe("Zwei")
    response = env.client.patch(
        f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h(),
        json={"title": "Soll nicht gelten", "reihenfolge": [a]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "reihenfolge_unvollstaendig"
    kurs = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h()).json()
    assert kurs["title"] == "PowerShell Grundlagen"


def test_aufgabe_anlegen_mit_position(env: Env) -> None:
    env.kurs()
    a = env.aufgabe("Eins")
    response = env.client.post(
        f"/api/v1/werkstatt/kurse/{env.course_id}/aufgaben", headers=env.h(),
        json={"chapter_title": "Kapitel 1", "summary": "Vorn", "position": 0},
    )
    assert response.status_code == 201
    kurs = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h()).json()
    assert [x["summary"] for x in kurs["aufgaben"]] == ["Vorn", "Eins"]
    assert a in [x["task_id"] for x in kurs["aufgaben"]]


def test_ausformulieren_legt_auftrag_an(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    response = env.client.post(
        f"/api/v1/werkstatt/aufgaben/{task_id}/ausformulieren", headers=env.h(),
        json={"based_on": None, "wunsch": "Mit Beispiel", "kind": "quiz"},
    )
    assert response.status_code == 202, response.text
    auftrag = werkstatt_routes._runtime().store.auftrag(response.json()["auftrag_id"])
    assert (auftrag["kind"], auftrag["task_id"], auftrag["course_id"]) == ("ausformulieren", task_id, env.course_id)
    assert auftrag["input"] == {"based_on": None, "wunsch": "Mit Beispiel", "kind": "quiz"}


def test_ausformulieren_auf_alter_basis_ist_409(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    response = env.client.post(
        f"/api/v1/werkstatt/aufgaben/{task_id}/ausformulieren", headers=env.h(), json={"based_on": None},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "konflikt"
    assert response.json()["neueste"]["number"] == 1


def test_patch_erste_fassung_ist_neu_ohne_basis(env: Env) -> None:
    env.kurs()
    fassung = env.erste_fassung(env.aufgabe())
    assert (fassung["number"], fassung["based_on"], fassung["art"]) == (1, None, "neu")
    assert fassung["author_ref"] == "anna"
    assert fassung["reason"] == "Aenderung"


def test_patch_nur_aufgabentext_ist_textkorrektur(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    response = env.patch(task_id, 1, {"prompt": "Mit welchem Befehl pruefen Sie, ob eine Datei existiert?"})
    assert response.status_code == 200, response.text
    assert (response.json()["number"], response.json()["based_on"], response.json()["art"]) == (2, 1, "textkorrektur")
    assert response.json()["content"]["options"][0]["text"] == "Test-Path"


def test_patch_neue_loesung_ist_neu(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    response = env.patch(task_id, 1, {"correct_option_ids": ["b"]})
    assert response.status_code == 200
    assert response.json()["art"] == "neu"


def test_patch_auf_alter_basis_gibt_409_mit_neuester(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    assert env.patch(task_id, 1, {"prompt": "Von Anna geaendert?"}).status_code == 200
    response = env.patch(task_id, 1, {"prompt": "Von Ben geaendert?"}, who=BEN)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "konflikt"
    assert body["neueste"]["number"] == 2
    assert body["neueste"]["author_ref"] == "anna"
    assert len(env.client.get(f"/api/v1/werkstatt/aufgaben/{task_id}/fassungen", headers=env.h()).json()["fassungen"]) == 2


def test_patch_ungueltiger_inhalt_ist_422(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    response = env.patch(task_id, 1, {"correct_option_ids": ["gibt-es-nicht"]})
    assert response.status_code == 422
    assert response.json()["code"] == "inhalt_ungueltig"
    assert env.client.get(f"/api/v1/werkstatt/aufgaben/{task_id}/fassungen", headers=env.h()).json()["fassungen"][-1]["number"] == 1


def test_patch_loest_quellenschluessel_auf_und_prueft(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    lektion = {
        "title": "Dateien pruefen",
        "body": f"{TEXT}\n\n```powershell\n{BEFEHL}\n```\n",
        "claims": [{"claim_id": "c1", "text": TEXT, "citation_ids": ["S1"]}],
        "concept_ids": ["k1"],
    }
    mit = env.patch(task_id, None, quiz(citation_ids=["S1"], lektion=lektion))
    assert mit.status_code == 200, mit.text
    inhalt = mit.json()["content"]
    assert "S1" not in inhalt["citation_ids"]
    assert len(inhalt["zitate"]) == 1
    assert inhalt["citation_ids"] == [inhalt["zitate"][0]["citation_id"]]
    assert "claim_uncited" not in [f["code"] for f in mit.json()["findings"]]

    ohne = env.patch(task_id, 1, {"lektion": {**lektion, "claims": [
        {"claim_id": "c1", "text": TEXT, "citation_ids": ["S9"]}]}})
    assert ohne.status_code == 200, ohne.text
    assert "claim_uncited" in [f["code"] for f in ohne.json()["findings"]]


def test_patch_nach_neuer_quellrevision_funktioniert(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    quelle = env.harness.ready_source
    neuer_hash = sha256(b"revision-2")
    text = f"Revision 2: mit {BEFEHL} pruefen Sie, ob eine Datei existiert."
    with env.harness.session_factory() as session, session.begin():
        artifact_id = str(uuid4())
        session.add(LearningArtifact(
            id=artifact_id, relative_path=f"sources/{quelle}-2.txt", content_hash=neuer_hash,
            media_type="text/plain", size_bytes=36, aggregate_type="source", aggregate_id=quelle,
            revision=2,
        ))
        session.add(LearningSourceRevision(
            source_id=quelle, revision=2, artifact_id=artifact_id, content_hash=neuer_hash,
            ingestion_spec_version="ingestion-v1", media_type="text/plain", size_bytes=36,
            status="stored",
        ))
        session.flush()
        session.add(LearningSourceChunk(
            id=str(uuid4()), source_id=quelle, source_revision=2, ordinal=0, content=text,
            content_hash=sha256(text.encode()), locator={"page": 1}, locator_hash=sha256(b"p"),
            metadata_json={},
        ))
        session.get(LearningSource, quelle).current_revision = 2

    response = env.patch(task_id, 1, {"prompt": "Mit welchem Befehl pruefen Sie eine Datei?"})
    assert response.status_code == 200, response.text
    kurs = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h()).json()
    assert kurs["source_provenance"] == [
        {"source_id": quelle, "revision": 2, "content_hash": neuer_hash}
    ]


def test_patch_ohne_bereite_quelle_ist_409_quellen_nicht_bereit(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    with env.harness.session_factory() as session, session.begin():
        session.get(LearningSourceRevision, (env.harness.ready_source, 1)).status = "failed"
    response = env.patch(task_id, 1, {"prompt": "Mit welchem Befehl pruefen Sie eine Datei?"})
    assert response.status_code == 409
    assert response.json()["code"] == "quellen_nicht_bereit"


def test_loeschen_und_zurueckholen(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    assert env.patch(task_id, 1, {"correct_option_ids": ["b"]}).status_code == 200

    assert env.client.delete(f"/api/v1/werkstatt/aufgaben/{task_id}", headers=env.h()).status_code == 200
    kurs = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h()).json()
    assert kurs["aufgaben"] == []
    gesperrt = env.patch(task_id, 2, {"prompt": "Darf nicht gehen?"})
    assert gesperrt.status_code == 409
    assert gesperrt.json()["code"] == "aufgabe_geloescht"

    zurueck = env.client.post(
        f"/api/v1/werkstatt/aufgaben/{task_id}/zurueckholen", headers=env.h(), json={"number": 1},
    )
    assert zurueck.status_code == 200, zurueck.text
    assert (zurueck.json()["number"], zurueck.json()["art"]) == (3, "zurueckgeholt")
    kurs = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h()).json()
    assert [a["task_id"] for a in kurs["aufgaben"]] == [task_id]
    fassungen = env.client.get(f"/api/v1/werkstatt/aufgaben/{task_id}/fassungen", headers=env.h()).json()["fassungen"]
    assert [f["number"] for f in fassungen] == [1, 2, 3]


def test_zurueckholen_unbekannter_nummer_ist_404(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    env.erste_fassung(task_id)
    response = env.client.post(
        f"/api/v1/werkstatt/aufgaben/{task_id}/zurueckholen", headers=env.h(), json={"number": 9},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "fassung_unbekannt"


def test_unbekannte_aufgabe_ist_404(env: Env) -> None:
    env.kurs()
    for method, path, body in (
        ("get", "/fassungen", None),
        ("delete", "", None),
        ("patch", "", {"based_on": None, "felder": {}, "reason": "x"}),
        ("post", "/ausformulieren", {"based_on": None}),
        ("post", "/zurueckholen", {"number": 1}),
    ):
        response = env.client.request(
            method, f"/api/v1/werkstatt/aufgaben/nope{path}", headers=env.h(), json=body
        ) if body is not None else env.client.request(
            method, f"/api/v1/werkstatt/aufgaben/nope{path}", headers=env.h()
        )
        assert response.status_code == 404, (method, path, response.text)
        assert response.json()["code"] == "aufgabe_unbekannt"


def test_unbekannter_auftrag_ist_404(env: Env) -> None:
    response = env.client.get("/api/v1/werkstatt/auftraege/nope", headers=env.h())
    assert response.status_code == 404
    assert response.json()["code"] == "auftrag_unbekannt"


def test_schreiben_ohne_autor_kopf_ist_400(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    for kopf in (None, {"X-Werkstatt-Author": "-kaputt"}, {"X-Werkstatt-Author": "a b"}):
        response = env.client.patch(
            f"/api/v1/werkstatt/aufgaben/{task_id}", headers=env.h(kopf),
            json={"based_on": None, "felder": quiz(), "reason": "x"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "autor_fehlt"
    response = env.client.post(
        f"/api/v1/werkstatt/kurse/{env.course_id}/aufgaben", headers=env.h(None),
        json={"chapter_title": "K", "summary": "S"},
    )
    assert response.status_code == 400 and response.json()["code"] == "autor_fehlt"


def test_lesen_braucht_keinen_autor_kopf(env: Env) -> None:
    env.kurs()
    response = env.client.get(f"/api/v1/werkstatt/kurse/{env.course_id}", headers=env.h(None))
    assert response.status_code == 200


def test_ohne_autorenrolle_ist_403(env: Env) -> None:
    for method, path in (
        ("get", f"/kurse/{env.course_id}"),
        ("post", f"/kurse/{env.course_id}/aufgaben"),
        ("get", "/auftraege/x"),
    ):
        response = env.client.request(
            method, f"/api/v1/werkstatt{path}", headers=env.h(AUTOR, roles="learning.reviewer"),
            **({"json": {"chapter_title": "K", "summary": "S"}} if method == "post" else {}),
        )
        assert response.status_code == 403, (method, path)


def test_vorschau_je_fassung(env: Env) -> None:
    env.kurs()
    task_id = env.aufgabe()
    url = f"/api/v1/werkstatt/aufgaben/{task_id}/vorschau"
    assert env.client.get(url, headers=env.h()).status_code == 404
    env.erste_fassung(task_id)
    assert env.patch(task_id, 1, {"correct_option_ids": ["b"]}).status_code == 200
    neueste = env.client.get(url, headers=env.h()).json()
    assert (neueste["number"], neueste["loesung"]) == (2, "Richtig: B")
    erste = env.client.get(url, headers=env.h(), params={"fassung": 1}).json()
    assert erste["loesung"] == "Richtig: A" and "Richtig" not in erste["text"]
    fehlt = env.client.get(url, headers=env.h(), params={"fassung": 9})
    assert (fehlt.status_code, fehlt.json()["code"]) == (404, "fassung_unbekannt")
    unbekannt = env.client.get("/api/v1/werkstatt/aufgaben/nope/vorschau", headers=env.h())
    assert (unbekannt.status_code, unbekannt.json()["code"]) == (404, "aufgabe_unbekannt")
