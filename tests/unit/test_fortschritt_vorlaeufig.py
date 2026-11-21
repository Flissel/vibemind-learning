"""Fortschritt: vorlaeufiger Wert nach der Abgabe, und Buchungen auf einem aelteren Kursstand zaehlen vorn mit.

Befund 21.11.: acht Abgaben in drei Tagen, keine gebucht (es fehlte „Passt“), also ueberall „—“. Und: eine
Sitzung auf Stand 10 buchte weiter dorthin, waehrend die Begriffsliste Stand 13 zeigte.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from spaces.learning.services.adaptive_engine.models import (
    AdaptiveConcept,
    AdaptiveItem,
    ConceptMastery,
    ItemConcept,
    ReviewSchedule,
)
from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService

from test_vorbewertung import (  # tests/unit ist kein Paket; pytest legt den Ordner auf den Suchpfad
    ACTOR,
    NOW,
    PNG,
    _bestaetigen,
    _choice,
    _falsch,
    _Leser,
    _richtig,
    _start,
    _store,
    _vor,
)

KEY = "rag.authority"


def _begriff(service: AdaptiveSessionService, course_id: str) -> dict[str, object]:
    (begriff,) = service.ueberblick(course_id=course_id, actor_id=ACTOR)["begriffe"]
    return begriff


def _stand_2(factory, course_id: str, specs: list[dict[str, object]]) -> str:
    """Ein zweiter Kursstand mit demselben Begriff; liefert die Kennung des Begriffs dort."""
    concept_id = str(uuid4())
    with factory() as db, db.begin():
        db.add(AdaptiveConcept(
            id=concept_id, course_id=course_id, course_revision=2, concept_key=KEY, title="Quellenautoritaet"))
        for index, spec in enumerate(specs, start=1):
            item_id = str(uuid4())
            db.add(AdaptiveItem(
                id=item_id, course_id=course_id, course_revision=2, activity_id=f"activity-{index}",
                difficulty=index / (len(specs) + 1), quality_status="approved", **spec))
            db.add(ItemConcept(item_id=item_id, concept_id=concept_id, course_id=course_id, course_revision=2))
    return concept_id


def _wert(factory, concept_id: str) -> tuple[float, float] | None:
    with factory() as db:
        m = db.get(ConceptMastery, (ACTOR, concept_id))
        return None if m is None else (m.alpha, m.beta)


# ---- Vorlaeufiger Wert ---------------------------------------------------------------------------------------


def test_ohne_abgabe_gibt_es_keinen_vorlaeufigen_wert(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=_Leser())
    _start(service, course_id)
    assert _begriff(service, course_id)["beherrschung"] is None
    assert _begriff(service, course_id)["vorlaeufig"] is None
    engine.dispose()


def test_abgabe_zeigt_vorlaeufig_was_passt_buchen_wuerde(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_richtig(turn)]
    vor = _vor(service, turn, handschrift_png=PNG)

    offen = _begriff(service, course_id)
    assert offen["beherrschung"] is None  # gebucht ist nichts
    assert isinstance(offen["vorlaeufig"], float) and offen["vorlaeufig"] > 0.5

    _bestaetigen(service, turn, vor.id)
    gebucht = _begriff(service, course_id)
    assert gebucht["beherrschung"] == offen["vorlaeufig"]  # genau der angekuendigte Wert
    assert gebucht["vorlaeufig"] is None  # nichts mehr offen
    engine.dispose()


def test_unlesbare_abgabe_zaehlt_nicht_vorlaeufig(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=_Leser(None))
    turn = _start(service, course_id)
    assert _vor(service, turn, handschrift_png=PNG).lesbar is False
    assert _begriff(service, course_id)["vorlaeufig"] is None
    engine.dispose()


def test_nur_die_neueste_abgabe_je_aufgabe_zaehlt_vorlaeufig(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    zeit = [NOW]
    service = AdaptiveSessionService(factory, clock=lambda: zeit[0], handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_falsch(turn)]
    _vor(service, turn, handschrift_png=PNG)
    nach_falsch = _begriff(service, course_id)["vorlaeufig"]
    zeit[0] = NOW + timedelta(minutes=1)
    leser._antworten = [_richtig(turn)]
    _vor(service, turn, handschrift_png=PNG + b"2")
    nach_richtig = _begriff(service, course_id)["vorlaeufig"]
    assert isinstance(nach_falsch, float) and isinstance(nach_richtig, float)
    assert nach_falsch < 0.5 < nach_richtig  # die zweite ersetzt die erste, sie addieren sich nicht
    engine.dispose()


def test_vorlaeufig_gilt_nur_der_eigenen_person(tmp_path: Path) -> None:
    engine, factory, course_id, _ = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_richtig(turn)]
    _vor(service, turn, handschrift_png=PNG)
    (fremd,) = service.ueberblick(course_id=course_id, actor_id="lernende-2")["begriffe"]
    assert fremd["beherrschung"] is None and fremd["vorlaeufig"] is None
    engine.dispose()


# ---- Buchung auf einem aelteren Kursstand ---------------------------------------------------------------------


def test_buchung_auf_altem_stand_zaehlt_auch_im_neuesten(tmp_path: Path) -> None:
    specs = [_choice(1), _choice(2)]
    engine, factory, course_id, alt = _store(tmp_path, specs)
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)  # Sitzung auf Stand 1
    neu = _stand_2(factory, course_id, specs)  # danach erscheint Stand 2, Loesungen unveraendert
    leser._antworten = [_richtig(turn)]
    vor = _vor(service, turn, handschrift_png=PNG)

    # Die Liste zeigt Stand 2; die offene Abgabe auf Stand 1 erscheint dort vorlaeufig.
    offen = _begriff(service, course_id)
    assert offen["beherrschung"] is None and isinstance(offen["vorlaeufig"], float)

    _bestaetigen(service, turn, vor.id)
    assert _wert(factory, alt) is not None and _wert(factory, neu) == _wert(factory, alt)
    assert _begriff(service, course_id)["beherrschung"] == offen["vorlaeufig"]
    with factory() as db:  # auch die Wiedervorlage steht vorn
        assert db.get(ReviewSchedule, (ACTOR, neu)) is not None
    engine.dispose()


def test_geaenderte_loesung_im_neuen_stand_bekommt_nichts_gutgeschrieben(tmp_path: Path) -> None:
    engine, factory, course_id, alt = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    neu = _stand_2(factory, course_id, [_choice(1), _choice(7)])  # eine Aufgabe hat eine andere Loesung
    leser._antworten = [_richtig(turn)]
    vor = _vor(service, turn, handschrift_png=PNG)
    assert _begriff(service, course_id)["vorlaeufig"] is None
    _bestaetigen(service, turn, vor.id)
    assert _wert(factory, alt) is not None and _wert(factory, neu) is None
    engine.dispose()


def test_eigener_wert_im_neuen_stand_wird_fortgeschrieben_nicht_ueberschrieben(tmp_path: Path) -> None:
    specs = [_choice(1), _choice(2)]
    engine, factory, course_id, alt = _store(tmp_path, specs)
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    neu = _stand_2(factory, course_id, specs)
    with factory() as db, db.begin():  # im neuen Stand steht schon mehr als im alten
        db.add(ConceptMastery(actor_id=ACTOR, concept_id=neu, alpha=5.0, beta=2.0, revision=4, updated_at=NOW))
    leser._antworten = [_richtig(turn)]
    vor = _vor(service, turn, handschrift_png=PNG)
    _bestaetigen(service, turn, vor.id)
    alpha, beta = _wert(factory, neu)  # type: ignore[misc]
    assert alpha > 5.0 and beta == 2.0
    assert _wert(factory, alt) != (alpha, beta)
    engine.dispose()


def test_buchung_im_neuesten_stand_bleibt_wie_bisher(tmp_path: Path) -> None:
    engine, factory, course_id, concept_id = _store(tmp_path, [_choice(1), _choice(2)])
    leser = _Leser()
    service = AdaptiveSessionService(factory, clock=lambda: NOW, handschrift_leser=leser)
    turn = _start(service, course_id)
    leser._antworten = [_richtig(turn)]
    vor = _vor(service, turn, handschrift_png=PNG)
    _bestaetigen(service, turn, vor.id)
    with factory() as db:
        assert len(db.scalars(select(ConceptMastery)).all()) == 1
    assert _wert(factory, concept_id) is not None
    engine.dispose()
