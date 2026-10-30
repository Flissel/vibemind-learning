"""Kursbewertungen: genau eine Zeile je (Kurs, Person); erneutes Abgeben ersetzt sie.

Nach außen verlässt nie eine actor_id den Speicher.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spaces.learning.services.bewertungen.models import KursBewertung
from spaces.learning.services.db.models import utc_now

SMILEYS = ("gut", "mittel", "schlecht")
STICHWORTE_MAX = 3
STICHWORT_LAENGE = 40
TITEL_MAX = 300


class BewertungFehler(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _iso(moment: datetime) -> str:
    # SQLite liefert Zeitstempel naiv zurück; sie wurden in UTC geschrieben.
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).isoformat()


def _bereinigt(stichworte: list[str]) -> list[str]:
    sauber = [s.strip() for s in stichworte if isinstance(s, str)]
    sauber = [s for s in sauber if s]
    if any(len(s) > STICHWORT_LAENGE for s in sauber):
        raise BewertungFehler("stichwort_zu_lang")
    if len(sauber) > STICHWORTE_MAX:
        raise BewertungFehler("zu_viele_stichworte")
    return sauber


def _verteilung(smileys: list[str]) -> dict[str, int]:
    zaehler = Counter(smileys)
    return {s: zaehler.get(s, 0) for s in SMILEYS}


class BewertungStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _zeile_holen(session: Session, course_id: str, actor_id: str) -> KursBewertung | None:
        return session.scalars(
            select(KursBewertung)
            .where(KursBewertung.course_id == course_id, KursBewertung.actor_id == actor_id)
            .with_for_update()
        ).first()

    def abgeben(
        self, course_id: str, actor_id: str, kurs_titel: str, smiley: str, stichworte: list[str]
    ) -> dict[str, Any]:
        if smiley not in SMILEYS:
            raise BewertungFehler("smiley_unbekannt")
        sauber = _bereinigt(stichworte)
        titel = (kurs_titel or "").strip()[:TITEL_MAX]
        try:
            return self._schreiben(course_id, actor_id, titel, smiley, sauber)
        except IntegrityError:
            # Eine parallele Absendung hat die Zeile gerade angelegt: als Update wiederholen.
            return self._schreiben(course_id, actor_id, titel, smiley, sauber)

    def _schreiben(
        self, course_id: str, actor_id: str, titel: str, smiley: str, stichworte: list[str]
    ) -> dict[str, Any]:
        with Session(self._engine) as session, session.begin():
            zeile = self._zeile_holen(session, course_id, actor_id)
            jetzt = utc_now()
            if zeile is None:
                zeile = KursBewertung(
                    id=str(uuid4()), course_id=course_id, actor_id=actor_id, kurs_titel=titel,
                    smiley=smiley, stichworte=stichworte, erstellt=jetzt, aktualisiert=jetzt,
                )
                session.add(zeile)
                session.flush()
                return {"id": zeile.id, "neu": True}
            if titel:
                zeile.kurs_titel = titel
            zeile.smiley = smiley
            zeile.stichworte = stichworte
            zeile.aktualisiert = jetzt
            zeile.gemeldet = None
            return {"id": zeile.id, "neu": False}

    def eigene(self, course_id: str, actor_id: str) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            zeile = session.scalars(
                select(KursBewertung).where(
                    KursBewertung.course_id == course_id, KursBewertung.actor_id == actor_id
                )
            ).first()
            if zeile is None:
                return None
            return {
                "smiley": zeile.smiley,
                "stichworte": list(zeile.stichworte),
                "aktualisiert": _iso(zeile.aktualisiert),
            }

    @staticmethod
    def _verteilungen(session: Session) -> dict[str, dict[str, int]]:
        je_kurs: dict[str, list[str]] = {}
        for course_id, smiley in session.execute(select(KursBewertung.course_id, KursBewertung.smiley)):
            je_kurs.setdefault(course_id, []).append(smiley)
        return {k: _verteilung(v) for k, v in je_kurs.items()}

    def neue(self, limit: int = 50) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            verteilungen = self._verteilungen(session)
            zeilen = session.scalars(
                select(KursBewertung)
                .where(KursBewertung.gemeldet.is_(None))
                .order_by(KursBewertung.aktualisiert, KursBewertung.id)
                .limit(limit)
            ).all()
            return [
                {
                    "id": z.id,
                    "aktualisiert": _iso(z.aktualisiert),
                    "course_id": z.course_id,
                    "kurs_titel": z.kurs_titel,
                    "smiley": z.smiley,
                    "stichworte": list(z.stichworte),
                    "geaendert": _iso(z.aktualisiert) > _iso(z.erstellt),
                    "verteilung": dict(verteilungen[z.course_id]),
                }
                for z in zeilen
            ]

    def gemeldet_markieren(self, eintraege: list[dict[str, str]]) -> int:
        """Markiert nur Zeilen, deren ``aktualisiert`` noch dem gemeldeten Stand entspricht."""
        markiert = 0
        with Session(self._engine) as session, session.begin():
            for eintrag in eintraege:
                zeile = session.scalars(
                    select(KursBewertung).where(KursBewertung.id == eintrag["id"]).with_for_update()
                ).first()
                if zeile is not None and _iso(zeile.aktualisiert) == eintrag["aktualisiert"]:
                    zeile.gemeldet = utc_now()
                    markiert += 1
        return markiert

    def uebersicht(self) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            zeilen = session.scalars(
                select(KursBewertung).order_by(KursBewertung.aktualisiert.desc(), KursBewertung.id)
            ).all()
            je_kurs: dict[str, list[KursBewertung]] = {}
            for z in zeilen:
                je_kurs.setdefault(z.course_id, []).append(z)
            ergebnis = []
            for course_id, liste in je_kurs.items():
                schreibweisen: dict[str, Counter[str]] = {}
                for z in reversed(liste):  # bei Gleichstand gewinnt die älteste Schreibweise
                    for wort in z.stichworte:
                        schreibweisen.setdefault(wort.casefold(), Counter())[wort] += 1
                top = sorted(
                    (
                        [schreib.most_common(1)[0][0], sum(schreib.values())]
                        for schreib in schreibweisen.values()
                    ),
                    key=lambda p: (-p[1], p[0].casefold()),
                )[:5]
                ergebnis.append(
                    {
                        "course_id": course_id,
                        "kurs_titel": liste[0].kurs_titel,
                        "verteilung": _verteilung([z.smiley for z in liste]),
                        "stichworte_top": top,
                        "einzeln": [
                            {"smiley": z.smiley, "stichworte": list(z.stichworte), "datum": _iso(z.aktualisiert)}
                            for z in liste
                        ],
                    }
                )
            ergebnis.sort(key=lambda k: k["kurs_titel"].casefold())
            return ergebnis
