"""Storage for the Werkstatt: courses, tasks and their numbered versions (Fassungen).

A task keeps its id for good; every change is a new Fassung. Writing a Fassung checks the
base against the newest number inside the same transaction, so two authors cannot both
silently win.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from spaces.learning.services.db.models import utc_now
from spaces.learning.services.werkstatt.models import (
    WerkstattAuftrag,
    WerkstattAufgabe,
    WerkstattFassung,
    WerkstattFreigabe,
    WerkstattKurs,
)


class WerkstattKonflikt(Exception):
    """The base of a write is stale; `neueste` is the newest Fassung."""

    def __init__(self, neueste: dict[str, Any]) -> None:
        super().__init__("fassung_veraltet")
        self.neueste = neueste


def _iso(moment: datetime) -> str:
    # SQLite hands timestamps back naive; they were written in UTC.
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).isoformat()


def _fassung_dict(f: WerkstattFassung) -> dict[str, Any]:
    return {
        "id": f.id, "task_id": f.task_id, "number": f.number, "based_on": f.based_on,
        "author_ref": f.author_ref, "reason": f.reason, "art": f.art,
        "content": dict(f.content), "findings": list(f.findings), "created_at": _iso(f.created_at),
    }


def _aufgabe_dict(a: WerkstattAufgabe, neueste: int | None = None) -> dict[str, Any]:
    return {
        "task_id": a.task_id, "course_id": a.course_id, "chapter_title": a.chapter_title,
        "position": a.position, "summary": a.summary, "deleted": a.deleted,
        "herkunft_key": a.herkunft_key, "lektion_herkunft_key": a.lektion_herkunft_key,
        "neueste_fassung": neueste, "created_at": _iso(a.created_at),
    }


def _kurs_dict(k: WerkstattKurs) -> dict[str, Any]:
    return {
        "course_id": k.course_id, "title": k.title, "audience": k.audience,
        "default_kind": k.default_kind, "outcomes": list(k.outcomes),
        "source_provenance": list(k.source_provenance), "owner_ref": k.owner_ref,
        "created_at": _iso(k.created_at), "updated_at": _iso(k.updated_at),
    }


def _freigabe_dict(f: WerkstattFreigabe) -> dict[str, Any]:
    return {
        "id": f.id, "course_id": f.course_id, "nummer": f.nummer, "zustand": f.zustand,
        "manifest": list(f.manifest), "vergleich": dict(f.vergleich), "befunde": list(f.befunde),
        "learnhouse_revision_vorher": f.learnhouse_revision_vorher,
        "learnhouse_revision": f.learnhouse_revision, "approval_ref": f.approval_ref,
        "angefordert_von": f.angefordert_von, "created_at": _iso(f.created_at),
        "freigegeben_at": _iso(f.freigegeben_at) if f.freigegeben_at else None,
    }


def _newest(session: Session, task_id: str) -> WerkstattFassung | None:
    return session.scalars(
        select(WerkstattFassung)
        .where(WerkstattFassung.task_id == task_id)
        .order_by(WerkstattFassung.number.desc())
        .limit(1)
    ).first()


class WerkstattStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    # courses

    def kurs_anlegen(
        self, course_id: str, *, title: str, audience: str, default_kind: str,
        outcomes: list[str], source_provenance: list[dict[str, Any]], owner_ref: str,
    ) -> dict[str, Any]:
        with self._sessions.begin() as session:
            if session.get(WerkstattKurs, course_id) is not None:
                raise ValueError("kurs_existiert")
            kurs = WerkstattKurs(
                course_id=course_id, title=title, audience=audience, default_kind=default_kind,
                outcomes=list(outcomes), source_provenance=list(source_provenance),
                owner_ref=owner_ref,
            )
            session.add(kurs)
            session.flush()
            return _kurs_dict(kurs)

    def kurs(self, course_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            kurs = session.get(WerkstattKurs, course_id)
            if kurs is None:
                raise LookupError("kurs_unbekannt")
            newest = dict(
                session.execute(
                    select(WerkstattFassung.task_id, func.max(WerkstattFassung.number))
                    .join(WerkstattAufgabe, WerkstattAufgabe.task_id == WerkstattFassung.task_id)
                    .where(WerkstattAufgabe.course_id == course_id)
                    .group_by(WerkstattFassung.task_id)
                ).all()
            )
            aufgaben = session.scalars(
                select(WerkstattAufgabe)
                .where(WerkstattAufgabe.course_id == course_id, WerkstattAufgabe.deleted.is_(False))
                .order_by(WerkstattAufgabe.position, WerkstattAufgabe.created_at)
            ).all()
            result = _kurs_dict(kurs)
            result["aufgaben"] = [_aufgabe_dict(a, newest.get(a.task_id)) for a in aufgaben]
            return result

    def kurs_aendern(
        self, course_id: str, *, title: str | None = None, default_kind: str | None = None
    ) -> dict[str, Any]:
        with self._sessions.begin() as session:
            kurs = session.get(WerkstattKurs, course_id)
            if kurs is None:
                raise LookupError("kurs_unbekannt")
            if title is not None:
                kurs.title = title
            if default_kind is not None:
                kurs.default_kind = default_kind
            kurs.updated_at = utc_now()
            session.flush()
            return _kurs_dict(kurs)

    def kurs_quellen_setzen(self, course_id: str, source_provenance: list[dict[str, Any]]) -> None:
        """Replace the pinned source revisions (the course follows the current source state)."""
        with self._sessions.begin() as session:
            kurs = session.get(WerkstattKurs, course_id)
            if kurs is None:
                raise LookupError("kurs_unbekannt")
            kurs.source_provenance = list(source_provenance)
            kurs.updated_at = utc_now()

    # tasks

    def aufgabe_anlegen(
        self, course_id: str, *, chapter_title: str, summary: str, position: int | None = None
    ) -> dict[str, Any]:
        with self._sessions.begin() as session:
            return self._aufgabe_einfuegen(session, course_id, chapter_title, summary, position)

    def grobentwurf_anwenden(
        self, course_id: str, *, title: str, default_kind: str, kapitel: list[dict[str, Any]]
    ) -> list[str]:
        """Apply a rough plan in ONE transaction: course title/kind plus all tasks, or nothing.

        kapitel is [{"title": str, "tasks": [{"summary": str}, ...]}, ...].
        """
        with self._sessions.begin() as session:
            kurs = session.get(WerkstattKurs, course_id)
            if kurs is None:
                raise LookupError("kurs_unbekannt")
            kurs.title = title
            kurs.default_kind = default_kind
            kurs.updated_at = utc_now()
            return [
                self._aufgabe_einfuegen(session, course_id, k["title"], t["summary"], None)["task_id"]
                for k in kapitel
                for t in k["tasks"]
            ]

    def uebernahme_anwenden(
        self, course_id: str, *, title: str, audience: str, default_kind: str,
        outcomes: list[str], source_provenance: list[dict[str, Any]], owner_ref: str,
        aufgaben: list[dict[str, Any]], author_ref: str,
    ) -> int:
        """Take over a published course in ONE transaction: course, tasks and Fassung 1, or nothing.

        aufgaben is [{"chapter_title": str, "summary": str, "content": dict, "findings": list}, ...] in order.
        """
        with self._sessions.begin() as session:
            if session.get(WerkstattKurs, course_id) is not None:
                raise ValueError("schon_uebernommen")
            session.add(WerkstattKurs(
                course_id=course_id, title=title, audience=audience, default_kind=default_kind,
                outcomes=list(outcomes), source_provenance=list(source_provenance),
                owner_ref=owner_ref,
            ))
            session.flush()
            for eintrag in aufgaben:
                aufgabe = self._aufgabe_einfuegen(
                    session, course_id, eintrag["chapter_title"], eintrag["summary"], None,
                    herkunft_key=eintrag.get("herkunft_key"),
                    lektion_herkunft_key=eintrag.get("lektion_herkunft_key"),
                )
                self._schreiben(
                    session, aufgabe["task_id"], content=eintrag["content"], based_on=None,
                    author_ref=author_ref, reason="Aus veroeffentlichtem Kurs uebernommen",
                    art="uebernommen", findings=list(eintrag.get("findings", [])),
                )
            return len(aufgaben)

    def _aufgabe_einfuegen(
        self, session: Session, course_id: str, chapter_title: str, summary: str,
        position: int | None, *, herkunft_key: str | None = None,
        lektion_herkunft_key: str | None = None,
    ) -> dict[str, Any]:
        if session.get(WerkstattKurs, course_id) is None:
            raise LookupError("kurs_unbekannt")
        alle = list(session.scalars(
            select(WerkstattAufgabe)
            .where(WerkstattAufgabe.course_id == course_id)
            .order_by(WerkstattAufgabe.position, WerkstattAufgabe.created_at)
        ))
        if position is None:
            # End of the chapter: after its last task, or at the end if the chapter is new.
            im_kapitel = [a.position for a in alle if a.chapter_title == chapter_title]
            position = max(im_kapitel) + 1 if im_kapitel else len(alle)
        position = max(0, min(position, len(alle)))
        for a in alle:
            if a.position >= position:
                a.position += 1
        aufgabe = WerkstattAufgabe(
            task_id=str(uuid4()), course_id=course_id, chapter_title=chapter_title,
            position=position, summary=summary, deleted=False,
            herkunft_key=herkunft_key, lektion_herkunft_key=lektion_herkunft_key,
        )
        session.add(aufgabe)
        session.flush()
        return _aufgabe_dict(aufgabe)

    def herkunft_setzen(
        self, zuordnung: dict[str, tuple[str | None, str | None]]
    ) -> int:
        """Fill EMPTY origin keys only: `{task_id: (herkunft_key, lektion_herkunft_key)}`.
        Returns the number of tasks that changed."""
        geaendert = 0
        with self._sessions.begin() as session:
            for task_id, (herkunft, lektion) in zuordnung.items():
                aufgabe = session.get(WerkstattAufgabe, task_id)
                if aufgabe is None:
                    continue
                neu = False
                if herkunft and not aufgabe.herkunft_key:
                    aufgabe.herkunft_key, neu = herkunft, True
                if lektion and not aufgabe.lektion_herkunft_key:
                    aufgabe.lektion_herkunft_key, neu = lektion, True
                geaendert += neu
        return geaendert

    def reihenfolge_setzen(self, course_id: str, task_ids: list[str]) -> None:
        with self._sessions.begin() as session:
            aufgaben = {
                a.task_id: a
                for a in session.scalars(
                    select(WerkstattAufgabe).where(
                        WerkstattAufgabe.course_id == course_id, WerkstattAufgabe.deleted.is_(False)
                    )
                )
            }
            if len(task_ids) != len(set(task_ids)) or set(task_ids) != set(aufgaben):
                raise ValueError("reihenfolge_unvollstaendig")
            for index, task_id in enumerate(task_ids):
                aufgaben[task_id].position = index

    # versions

    def fassung_anlegen(
        self, task_id: str, *, content: dict[str, Any], based_on: int | None, author_ref: str,
        reason: str, art: str, findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._sessions.begin() as session:
            aufgabe = session.get(WerkstattAufgabe, task_id)
            if aufgabe is None:
                raise LookupError("aufgabe_unbekannt")
            if aufgabe.deleted:
                raise ValueError("aufgabe_geloescht")
            return self._schreiben(
                session, task_id, content=content, based_on=based_on, author_ref=author_ref,
                reason=reason, art=art, findings=findings,
            )

    def _schreiben(
        self, session: Session, task_id: str, *, content: dict[str, Any], based_on: int | None,
        author_ref: str, reason: str, art: str, findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        newest = _newest(session, task_id)
        if (newest.number if newest else None) != based_on:
            # Without a version there is nothing newer to show: an empty dict says so.
            raise WerkstattKonflikt(_fassung_dict(newest) if newest else {})
        fassung = WerkstattFassung(
            id=str(uuid4()), task_id=task_id, number=(newest.number + 1) if newest else 1,
            based_on=based_on, author_ref=author_ref, reason=reason, art=art,
            content=dict(content), findings=list(findings),
        )
        session.add(fassung)
        session.flush()
        return _fassung_dict(fassung)

    def fassungen(self, task_id: str) -> list[dict[str, Any]]:
        with self._sessions() as session:
            return [
                _fassung_dict(f)
                for f in session.scalars(
                    select(WerkstattFassung)
                    .where(WerkstattFassung.task_id == task_id)
                    .order_by(WerkstattFassung.number)
                )
            ]

    def neueste(self, task_id: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            newest = _newest(session, task_id)
            return _fassung_dict(newest) if newest else None

    def zurueckholen(self, task_id: str, number: int, *, author_ref: str) -> dict[str, Any]:
        with self._sessions.begin() as session:
            aufgabe = session.get(WerkstattAufgabe, task_id)
            if aufgabe is None:
                raise LookupError("aufgabe_unbekannt")
            alt = session.scalars(
                select(WerkstattFassung).where(
                    WerkstattFassung.task_id == task_id, WerkstattFassung.number == number
                )
            ).first()
            if alt is None:
                raise LookupError("fassung_unbekannt")
            newest = _newest(session, task_id)
            assert newest is not None  # `alt` exists, so there is at least one version
            aufgabe.deleted = False
            return self._schreiben(
                session, task_id, content=alt.content, based_on=newest.number,
                author_ref=author_ref, reason=f"Fassung {number} zurückgeholt",
                art="zurueckgeholt", findings=[],
            )

    def aufgabe_loeschen(self, task_id: str, *, author_ref: str) -> None:
        # Deleting only hides the task; its Fassungen stay and it can be brought back.
        with self._sessions.begin() as session:
            aufgabe = session.get(WerkstattAufgabe, task_id)
            if aufgabe is None:
                raise LookupError("aufgabe_unbekannt")
            aufgabe.deleted = True

    def aufgaben_alle(self, course_id: str) -> list[dict[str, Any]]:
        """All tasks of a course, deleted ones included, in course order."""
        with self._sessions() as session:
            return [
                _aufgabe_dict(a)
                for a in session.scalars(
                    select(WerkstattAufgabe)
                    .where(WerkstattAufgabe.course_id == course_id)
                    .order_by(WerkstattAufgabe.position, WerkstattAufgabe.created_at)
                )
            ]

    # releases (Freigaben)

    def freigabe_anlegen(
        self, course_id: str, *, manifest: list[dict[str, Any]], vergleich: dict[str, Any],
        befunde: list[dict[str, Any]], learnhouse_revision_vorher: int | None, autor: str,
    ) -> dict[str, Any]:
        """New request; an older open one of the course is discarded in the same transaction.

        Two requests at once can draw the same `nummer`; the unique key rejects the second,
        which is retried once with a fresh maximum."""
        for versuch in (1, 2):
            try:
                return self._freigabe_einfuegen(
                    course_id, manifest, vergleich, befunde, learnhouse_revision_vorher, autor
                )
            except IntegrityError:
                if versuch == 2:
                    raise ValueError("freigabe_gleichzeitig") from None
        raise AssertionError("unreachable")

    def _naechste_nummer(self, session: Session, course_id: str) -> int:
        hoechste = session.scalar(
            select(func.max(WerkstattFreigabe.nummer))
            .where(WerkstattFreigabe.course_id == course_id)
        )
        return (hoechste or 0) + 1

    def _freigabe_einfuegen(
        self, course_id: str, manifest: list[dict[str, Any]], vergleich: dict[str, Any],
        befunde: list[dict[str, Any]], learnhouse_revision_vorher: int | None, autor: str,
    ) -> dict[str, Any]:
        with self._sessions.begin() as session:
            if session.get(WerkstattKurs, course_id) is None:
                raise LookupError("kurs_unbekannt")
            session.execute(
                update(WerkstattFreigabe)
                .where(WerkstattFreigabe.course_id == course_id,
                       WerkstattFreigabe.zustand == "angefordert")
                .values(zustand="verworfen")
            )
            freigabe = WerkstattFreigabe(
                id=str(uuid4()), course_id=course_id,
                nummer=self._naechste_nummer(session, course_id), zustand="angefordert",
                manifest=list(manifest),  # type: ignore[arg-type]  # the column holds a JSON list
                vergleich=dict(vergleich), befunde=list(befunde),
                learnhouse_revision_vorher=learnhouse_revision_vorher, angefordert_von=autor,
            )
            session.add(freigabe)
            session.flush()
            return _freigabe_dict(freigabe)

    def freigabe(self, freigabe_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            freigabe = session.get(WerkstattFreigabe, freigabe_id)
            if freigabe is None:
                raise LookupError("freigabe_unbekannt")
            return _freigabe_dict(freigabe)

    def freigabe_letzte_freigegebene(self, course_id: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            freigabe = session.scalars(
                select(WerkstattFreigabe)
                .where(WerkstattFreigabe.course_id == course_id,
                       WerkstattFreigabe.zustand == "freigegeben")
                .order_by(WerkstattFreigabe.nummer.desc())
                .limit(1)
            ).first()
            return _freigabe_dict(freigabe) if freigabe else None

    def freigabe_offene(self, course_id: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            freigabe = session.scalars(
                select(WerkstattFreigabe)
                .where(WerkstattFreigabe.course_id == course_id,
                       WerkstattFreigabe.zustand == "angefordert")
                .order_by(WerkstattFreigabe.nummer.desc())
                .limit(1)
            ).first()
            return _freigabe_dict(freigabe) if freigabe else None

    def freigabe_verwerfen(self, freigabe_id: str) -> None:
        with self._sessions.begin() as session:
            freigabe = session.get(WerkstattFreigabe, freigabe_id)
            if freigabe is None:
                raise LookupError("freigabe_unbekannt")
            if freigabe.zustand != "angefordert":
                raise ValueError("freigabe_nicht_offen")
            freigabe.zustand = "verworfen"

    # jobs (model work that outlives one HTTP request)

    def auftrag_anlegen(
        self, course_id: str, *, kind: str, input: dict[str, Any], task_id: str | None = None
    ) -> dict[str, Any]:
        with self._sessions.begin() as session:
            auftrag = WerkstattAuftrag(
                id=str(uuid4()), course_id=course_id, task_id=task_id, kind=kind,
                input=dict(input), state="queued",
            )
            session.add(auftrag)
            session.flush()
            return _auftrag_dict(auftrag)

    def grobentwurf_neu(self, course_id: str, *, hinweise: list[str]) -> dict[str, Any]:
        """Queue a fresh rough draft, e.g. after a failed one.

        Allowed only while the course has no (non-deleted) task and no rough draft is queued or
        running; otherwise ValueError("grobentwurf_nicht_moeglich"). Check and insert share one
        transaction; the course row is locked so two requests cannot both queue one."""
        with self._sessions.begin() as session:
            kurs = session.get(WerkstattKurs, course_id, with_for_update=True)
            if kurs is None:
                raise LookupError("kurs_unbekannt")
            aufgabe = session.scalars(
                select(WerkstattAufgabe.task_id)
                .where(WerkstattAufgabe.course_id == course_id, WerkstattAufgabe.deleted.is_(False))
                .limit(1)
            ).first()
            offen = session.scalars(
                select(WerkstattAuftrag.id)
                .where(
                    WerkstattAuftrag.course_id == course_id,
                    WerkstattAuftrag.kind == "grobentwurf",
                    WerkstattAuftrag.state.in_(("queued", "running")),
                )
                .limit(1)
            ).first()
            if aufgabe is not None or offen is not None:
                raise ValueError("grobentwurf_nicht_moeglich")
            auftrag = WerkstattAuftrag(
                id=str(uuid4()), course_id=course_id, task_id=None, kind="grobentwurf",
                input={"hinweise": list(hinweise)}, state="queued",
            )
            session.add(auftrag)
            session.flush()
            return _auftrag_dict(auftrag)

    def auftrag(self, auftrag_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            auftrag = session.get(WerkstattAuftrag, auftrag_id)
            if auftrag is None:
                raise LookupError("auftrag_unbekannt")
            return _auftrag_dict(auftrag)

    def naechster_auftrag(self) -> dict[str, Any] | None:
        """Claim the oldest queued job by flipping queued -> running.

        The flip is a guarded UPDATE (`WHERE state = 'queued'`); only the caller whose UPDATE
        changed a row owns the job, so two threads or processes can never take the same one.
        Works the same on Postgres and SQLite; a lost race just tries the next candidate.
        """
        while True:
            with self._sessions.begin() as session:
                kandidat = session.scalars(
                    select(WerkstattAuftrag.id)
                    .where(WerkstattAuftrag.state == "queued")
                    .order_by(WerkstattAuftrag.created_at, WerkstattAuftrag.id)
                    .limit(1)
                ).first()
                if kandidat is None:
                    return None
                geflippt = session.execute(
                    update(WerkstattAuftrag)
                    .where(WerkstattAuftrag.id == kandidat, WerkstattAuftrag.state == "queued")
                    .values(state="running")
                ).rowcount
                if geflippt == 1:
                    session.expire_all()
                    return _auftrag_dict(session.get(WerkstattAuftrag, kandidat))

    def auftrag_fertig(self, auftrag_id: str, result: dict[str, Any]) -> None:
        self._abschliessen(auftrag_id, "fertig", result=result, error=None)

    def auftrag_fehler(
        self, auftrag_id: str, error: str, result: dict[str, Any] | None = None
    ) -> None:
        self._abschliessen(auftrag_id, "fehler", result=result, error=error)

    def haengende_auftraege_abbrechen(self) -> int:
        """Jobs still `running` belong to a dead process: end them as `abgebrochen`."""
        with self._sessions.begin() as session:
            return session.execute(
                update(WerkstattAuftrag)
                .where(WerkstattAuftrag.state == "running")
                .values(state="fehler", error="abgebrochen", finished_at=utc_now())
            ).rowcount

    def _abschliessen(
        self, auftrag_id: str, state: str, *, result: dict[str, Any] | None, error: str | None
    ) -> None:
        with self._sessions.begin() as session:
            auftrag = session.get(WerkstattAuftrag, auftrag_id)
            if auftrag is None:
                raise LookupError("auftrag_unbekannt")
            auftrag.state = state
            auftrag.result = result
            auftrag.error = error
            auftrag.finished_at = utc_now()


def _auftrag_dict(a: WerkstattAuftrag) -> dict[str, Any]:
    return {
        "id": a.id, "course_id": a.course_id, "task_id": a.task_id, "kind": a.kind,
        "input": dict(a.input), "state": a.state,
        "result": dict(a.result) if a.result is not None else None, "error": a.error,
        "created_at": _iso(a.created_at),
        "finished_at": _iso(a.finished_at) if a.finished_at else None,
    }
