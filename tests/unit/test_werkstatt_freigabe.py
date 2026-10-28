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


def _befund(r: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [b for b in r["befunde"] if b["code"] == code]


def test_konzept_ohne_lektion_ist_hinweis_nicht_sperre(tmp_path):
    s = _store(tmp_path)
    ohne_lektion = _quiz()
    ohne_lektion.pop("lektion")
    ohne_lektion["concept_ids"] = ["k-ohne"]
    _aufgabe(s, _quiz())
    _aufgabe(s, ohne_lektion)
    r = _vorbereiten(s)
    b = _befund(r, "concept_missing_lesson")
    assert len(b) == 1 and "k-ohne" in b[0]["text"] and b[0]["blockierend"] is False
    assert r["blockiert"] is False


def test_typverteilung_ist_hinweis(tmp_path):
    s = _store(tmp_path)
    for i in range(4):
        _aufgabe(s, _quiz(f"Frage {i}?"))
    r = _vorbereiten(s)
    b = _befund(r, "task_type_distribution")
    assert len(b) == 1 and b[0]["blockierend"] is False
    assert "quiz" in b[0]["text"] and "100" in b[0]["text"]
    assert r["blockiert"] is False


def test_du_anrede_sperrt_die_freigabe(tmp_path):
    s = _store(tmp_path)
    b = _quiz("Zweite Frage?")
    b["lektion"]["body"] = "Dazu sagen wir, dass du cd verwenden kannst."
    _aufgabe(s, _quiz())
    _aufgabe(s, b)
    r = _vorbereiten(s)
    assert _befund(r, "address_mixed")[0]["blockierend"] is True
    assert r["blockiert"] is True


def test_aufgabenbefund_ist_blockierend_markiert(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz(), findings=[{"code": "quiz_all_but_one", "text": "Raten."}])
    assert _befund(_vorbereiten(s), "aufgabe_mit_befunden")[0]["blockierend"] is True


def test_zurueckgeholt_auf_freigegebenen_stand_ist_unveraendert(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz())
    _freigeben(s, _vorbereiten(s)["freigabe_id"])
    _neue_fassung(s, t1, _quiz(richtig="b"))
    s.zurueckholen(t1, 1, author_ref="kursleitung-a")
    r = _vorbereiten(s)
    assert s.neueste(t1)["number"] == 3
    assert r["vergleich"]["unveraendert"] == 1
    assert all(r["vergleich"][k] == [] for k in ("neu", "geaendert", "textkorrektur", "geloescht"))
    assert r["nichts_zu_veroeffentlichen"] is True


def test_zurueckgeholt_nach_loeschen_mit_freigegebenem_inhalt_ist_unveraendert(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz())
    _freigeben(s, _vorbereiten(s)["freigabe_id"])
    s.aufgabe_loeschen(t1, author_ref="kursleitung-a")
    s.zurueckholen(t1, 1, author_ref="kursleitung-a")
    r = _vorbereiten(s)
    assert r["vergleich"]["unveraendert"] == 1 and r["nichts_zu_veroeffentlichen"] is True


def test_textkorrektur_bleibt_textkorrektur_mit_inhalts_hash(tmp_path):
    s = _store(tmp_path)
    t1 = _aufgabe(s, _quiz())
    _freigeben(s, _vorbereiten(s)["freigabe_id"])
    _neue_fassung(s, t1, _quiz("Anderer Wortlaut?"), "textkorrektur")
    r = _vorbereiten(s)
    assert [e["task_id"] for e in r["vergleich"]["textkorrektur"]] == [t1]
    assert len(s.freigabe_offene(COURSE)["manifest"][0]["inhalt_hash"]) == 64


def test_ungueltiger_inhalt_blockiert_und_fehlt_im_manifest(tmp_path):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    kaputt = _aufgabe(s, {"kind": "exercise", "prompt": "kein gueltiger Typ"})
    r = _vorbereiten(s)
    b = _befund(r, "aufgabe_mit_befunden")
    assert [x["task_id"] for x in b] == [kaputt] and "ungültig" in b[0]["text"]
    assert b[0]["blockierend"] is True and r["blockiert"] is True
    assert kaputt not in [m["task_id"] for m in s.freigabe_offene(COURSE)["manifest"]]


def test_nummernkollision_wird_einmal_wiederholt(tmp_path, monkeypatch):
    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    _vorbereiten(s)
    echte = s._naechste_nummer
    aufrufe: list[int] = []

    def veraltet(session, course_id):
        aufrufe.append(1)
        return 1 if len(aufrufe) == 1 else echte(session, course_id)

    monkeypatch.setattr(s, "_naechste_nummer", veraltet)
    assert _vorbereiten(s)["nummer"] == 2 and len(aufrufe) == 2


def test_dauerhafte_nummernkollision_meldet_freigabe_gleichzeitig(tmp_path, monkeypatch):
    import pytest

    s = _store(tmp_path)
    _aufgabe(s, _quiz())
    _vorbereiten(s)
    monkeypatch.setattr(s, "_naechste_nummer", lambda session, course_id: 1)
    with pytest.raises(ValueError, match="freigabe_gleichzeitig"):
        _vorbereiten(s)
    assert s.freigabe_offene(COURSE)["nummer"] == 1  # nothing was discarded by the failed try
