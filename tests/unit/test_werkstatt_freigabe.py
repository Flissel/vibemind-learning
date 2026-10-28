"""Freigabe vorbereiten: Manifest, Vergleich mit der letzten Freigabe, kursweite Regeln."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from spaces.learning.services.db.models import Base
from spaces.learning.services.werkstatt.freigabe import (
    freigabe_vorbereiten,
    loesung_hash,
)
from spaces.learning.services.werkstatt.models import WerkstattFreigabe
from spaces.learning.services.werkstatt.store import WerkstattStore

COURSE = "c1"


def _store(tmp_path: Path) -> WerkstattStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'freigabe.db'}")
    Base.metadata.create_all(engine)
    s = WerkstattStore(sessionmaker(bind=engine, expire_on_commit=False))
    s.kurs_anlegen(
        COURSE, title="Terminal", audience="Einsteiger", default_kind="quiz",
        outcomes=["Befehle eingeben"], source_provenance=[], owner_ref="kursleitung-a",
    )
    return s


def _quiz(frage: str = "Welcher Befehl listet Dateien auf?", richtig: str = "a") -> dict[str, Any]:
    return {
        "kind": "quiz", "prompt": frage, "concept_ids": ["k1"],
        "options": [{"option_id": "a", "text": "ls"}, {"option_id": "b", "text": "cd"}],
        "correct_option_ids": [richtig],
        "lektion": {
            "title": "Dateien", "concept_ids": ["k1"],
            "body": "Mit ls zeigen Sie die Dateien eines Ordners an.",
        },
    }


def _aufgabe(s: WerkstattStore, content: dict[str, Any] | None, *, kapitel: str = "Kapitel 1",
             findings: list[dict[str, str]] | None = None) -> str:
    t = s.aufgabe_anlegen(COURSE, chapter_title=kapitel, summary=f"Aufgabe {kapitel}")
    if content is not None:
        s.fassung_anlegen(
            t["task_id"], content=content, based_on=None, author_ref="kursleitung-a",
            reason="neu", art="neu", findings=findings or [],
        )
    return t["task_id"]


def _neue_fassung(s: WerkstattStore, task_id: str, content: dict[str, Any], art: str = "neu") -> None:
    s.fassung_anlegen(
        task_id, content=content, based_on=s.neueste(task_id)["number"],
        author_ref="kursleitung-a", reason="r", art=art, findings=[],
    )


def _vorbereiten(s: WerkstattStore, revision: int = 1) -> dict[str, Any]:
    return freigabe_vorbereiten(s, COURSE, autor="kursleitung-a", learnhouse_revision=revision)


def _freigeben(s: WerkstattStore, freigabe_id: str) -> None:
    # Task 5 adds the real "mark freigegeben"; here the row is flipped directly.
    with s._sessions.begin() as session:
        session.execute(
            update(WerkstattFreigabe).where(WerkstattFreigabe.id == freigabe_id)
            .values(zustand="freigegeben")
        )


def test_erste_freigabe_ist_alles_neu(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz())
    t2 = _aufgabe(s, _quiz("Wie wechselt man den Ordner?", "b"))
    r = _vorbereiten(s)
    assert r["nummer"] == 1
    assert [e["task_id"] for e in r["vergleich"]["neu"]] == [t1, t2]
    assert r["vergleich"]["geaendert"] == [] and r["vergleich"]["unveraendert"] == 0
    assert r["blockiert"] is False and r["nichts_zu_veroeffentlichen"] is False
    eintrag = r["vergleich"]["neu"][0]
    assert eintrag["kapitel"] == "Kapitel 1" and eintrag["summary"] == "Aufgabe Kapitel 1"
    assert eintrag["fassung_neu"] == 1 and eintrag["fassung_alt"] is None
    offen = s.freigabe_offene(COURSE)
    assert offen["id"] == r["freigabe_id"] and offen["zustand"] == "angefordert"
    assert offen["learnhouse_revision_vorher"] == 1
    m = offen["manifest"][0]
    assert m["task_id"] == t1 and m["fassung"] == 1 and m["aufgaben_key"] == f"werkstatt:{t1}"
    assert m["lektion_key"] == f"werkstatt:{t1}:lektion" and m["position"] == 0
    assert len(m["loesung_hash"]) == 64


def test_textkorrektur_und_neu_werden_getrennt(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz())
    t2 = _aufgabe(s, _quiz("Zweite Frage?"))
    t3 = _aufgabe(s, _quiz("Dritte Frage?"))
    _freigeben(s, _vorbereiten(s)["freigabe_id"])
    _neue_fassung(s, t1, _quiz("Welcher Befehl zeigt Dateien an?"), "textkorrektur")
    _neue_fassung(s, t2, _quiz("Zweite Frage?", richtig="b"))
    r = _vorbereiten(s)
    assert r["nummer"] == 2
    assert [(e["task_id"], e["fassung_alt"], e["fassung_neu"]) for e in r["vergleich"]["textkorrektur"]] == [(t1, 1, 2)]
    assert [(e["task_id"], e["fassung_alt"], e["fassung_neu"]) for e in r["vergleich"]["geaendert"]] == [(t2, 1, 2)]
    assert r["vergleich"]["unveraendert"] == 1 and r["vergleich"]["neu"] == []
    assert t3 not in [e["task_id"] for k in ("neu", "geaendert", "textkorrektur") for e in r["vergleich"][k]]
    assert r["nichts_zu_veroeffentlichen"] is False


def test_geloeschte_aufgabe_erscheint_im_vergleich(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz())
    _aufgabe(s, _quiz("Zweite Frage?"))
    _freigeben(s, _vorbereiten(s)["freigabe_id"])
    s.aufgabe_loeschen(t1, author_ref="kursleitung-a")
    r = _vorbereiten(s)
    assert [e["task_id"] for e in r["vergleich"]["geloescht"]] == [t1]
    g = r["vergleich"]["geloescht"][0]
    assert g["kapitel"] == "Kapitel 1" and g["summary"] == "Aufgabe Kapitel 1" and g["fassung_alt"] == 1
    assert g["fassung_neu"] is None
    assert r["nichts_zu_veroeffentlichen"] is False
    assert t1 not in [m["task_id"] for m in s.freigabe_offene(COURSE)["manifest"]]


def test_unveraenderter_stand_ist_nichts_zu_veroeffentlichen(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    _aufgabe(s, _quiz("Zweite Frage?"))
    _freigeben(s, _vorbereiten(s)["freigabe_id"])
    r = _vorbereiten(s)
    assert r["vergleich"]["unveraendert"] == 2
    assert all(r["vergleich"][k] == [] for k in ("neu", "geaendert", "textkorrektur", "geloescht"))
    assert r["nichts_zu_veroeffentlichen"] is True and r["blockiert"] is False


def test_aufgabe_mit_befund_blockiert(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz(), findings=[{"code": "quiz_all_but_one", "text": "Raten moeglich."}])
    r = _vorbereiten(s)
    assert r["blockiert"] is True
    b = [x for x in r["befunde"] if x["code"] == "aufgabe_mit_befunden"]
    assert len(b) == 1 and b[0]["task_id"] == t1 and "Raten moeglich." in b[0]["text"]


def test_befund_ohne_blockade_blockiert_nicht(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz(), findings=[{"code": "lesson_body_thin", "text": "Duenn."}])
    assert _vorbereiten(s)["blockiert"] is False


def test_aufgabe_ohne_fassung_blockiert(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    leer = _aufgabe(s, None)
    r = _vorbereiten(s)
    assert r["blockiert"] is True
    b = [x for x in r["befunde"] if x["code"] == "aufgabe_ohne_fassung"]
    assert [x["task_id"] for x in b] == [leer]
    assert leer not in [m["task_id"] for m in s.freigabe_offene(COURSE)["manifest"]]


def test_kurs_ohne_aufgaben_blockiert(tmp_path):
    s = _store(tmp_path)
    r = _vorbereiten(s)
    assert r["blockiert"] is True and [x["code"] for x in r["befunde"]] == ["kurs_ohne_aufgaben"]


def test_kursweite_regeln_laufen_mit(tmp_path):
    s = _store(tmp_path)
    b = _quiz("Zweite Frage?")
    b["lektion"]["body"] = "Dazu sagen wir, dass du cd verwenden kannst."
    _aufgabe(s, _quiz())
    _aufgabe(s, b)
    r = _vorbereiten(s)
    assert "address_mixed" in [x["code"] for x in r["befunde"]]
    assert r["blockiert"] is True


def test_neue_anforderung_verwirft_die_alte(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    erste = _vorbereiten(s)
    zweite = _vorbereiten(s)
    assert (erste["nummer"], zweite["nummer"]) == (1, 2)
    assert s.freigabe(erste["freigabe_id"])["zustand"] == "verworfen"
    assert s.freigabe_offene(COURSE)["id"] == zweite["freigabe_id"]
    # discarded ones still count for the number
    assert _vorbereiten(s)["nummer"] == 3


def test_vergleich_geht_gegen_letzte_freigegebene_nicht_gegen_verworfene(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz())
    _vorbereiten(s)  # verworfen by the next one, never freigegeben
    r = _vorbereiten(s)
    assert [e["task_id"] for e in r["vergleich"]["neu"]] == [t1]


def test_loesung_hash_gleich_bei_textkorrektur():
    assert loesung_hash(_quiz()) == loesung_hash(_quiz("Welcher Befehl zeigt Dateien an?"))
    assert loesung_hash(_quiz()) != loesung_hash(_quiz(richtig="b"))
    anders = _quiz()
    anders["options"] = list(reversed(anders["options"]))
    assert loesung_hash(anders) == loesung_hash(_quiz())
