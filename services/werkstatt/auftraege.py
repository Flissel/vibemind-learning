"""Werkstatt worker: runs queued model jobs (rough plan, one task) in its own thread.

Each job gets a fresh event loop (`asyncio.run`) and a fresh gateway that is closed inside that
same loop. The OpenFang client pools connections per loop, so a gateway must not outlive the
loop that opened it; one loop per job keeps the thread independent of the factory loop in the
main thread. A failing job is marked `fehler` and never stops the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from pydantic import ValidationError

from spaces.learning.services.course_factory.model_gateway import (
    GatewayRejected,
    GatewaySchemaError,
    GatewayUnavailable,
    ModelGateway,
)
from spaces.learning.services.course_factory.repository import SourceProvenance
from spaces.learning.services.werkstatt.autor import (
    aufgabe_ausformulieren,
    grobentwurf_erzeugen,
)
from spaces.learning.services.werkstatt.inhalt import fassungs_art
from spaces.learning.services.werkstatt.store import WerkstattKonflikt, WerkstattStore

logger = logging.getLogger(__name__)

AUTOR_REF = "werkstatt-modell"


class _Katalog(Protocol):
    def load(self, *, course_id: str, provenance: tuple[SourceProvenance, ...]) -> Any: ...


class _Fehler(Exception):
    """A job failure with a short code (and optionally a result payload)."""

    def __init__(self, code: str, result: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.result = result


class WerkstattArbeiter:
    def __init__(
        self,
        store: WerkstattStore,
        gateway_factory: Callable[[], Any],
        catalog_loader: _Katalog,
    ) -> None:
        self._store = store
        self._gateway_factory = gateway_factory
        self._catalog = catalog_loader

    def haengende_abbrechen(self) -> int:
        return self._store.haengende_auftraege_abbrechen()

    async def einen_ausfuehren(self) -> bool:
        auftrag = self._store.naechster_auftrag()
        if auftrag is None:
            return False
        gateway = None
        try:
            gateway = self._gateway_factory()
            result = await self._ausfuehren(auftrag, gateway)
            self._store.auftrag_fertig(auftrag["id"], result)
        except _Fehler as fehler:
            self._store.auftrag_fehler(auftrag["id"], fehler.code, fehler.result)
        except (GatewayUnavailable, GatewayRejected):
            logger.warning("werkstatt_modell_nicht_erreichbar", extra={"auftrag": auftrag["id"]})
            self._store.auftrag_fehler(auftrag["id"], "modell_nicht_erreichbar")
        except (GatewaySchemaError, ValidationError):
            logger.warning("werkstatt_modell_ausgabe_ungueltig", extra={"auftrag": auftrag["id"]})
            self._store.auftrag_fehler(auftrag["id"], "modell_ausgabe_ungueltig")
        except Exception:
            logger.exception("werkstatt_auftrag_fehlgeschlagen", extra={"auftrag": auftrag["id"]})
            self._store.auftrag_fehler(auftrag["id"], "interner_fehler")
        finally:
            if gateway is not None:
                try:
                    await gateway.aclose()
                except Exception:
                    logger.exception("werkstatt_gateway_schliessen_fehlgeschlagen")
        return True

    def run_forever(self, poll_s: float = 1.0) -> None:
        try:
            self.haengende_abbrechen()
        except Exception:
            logger.exception("werkstatt_haengende_abbrechen_fehlgeschlagen")
        while True:
            try:
                arbeitete = asyncio.run(self.einen_ausfuehren())
            except Exception:
                logger.exception("werkstatt_schleife_fehlgeschlagen")
                arbeitete = False
            if not arbeitete:
                time.sleep(poll_s)

    # jobs

    def _quellen(self, kurs: dict[str, Any]) -> Any:
        provenance = tuple(
            SourceProvenance(
                source_id=str(p["source_id"]),
                revision=int(p["revision"]),
                content_hash=str(p["content_hash"]),
            )
            for p in kurs["source_provenance"]
        )
        try:
            return self._catalog.load(course_id=kurs["course_id"], provenance=provenance)
        except ValueError as error:
            raise _Fehler("quellen_veraltet") from error

    async def _ausfuehren(self, auftrag: dict[str, Any], gateway: ModelGateway) -> dict[str, Any]:
        kurs = self._store.kurs(auftrag["course_id"])
        katalog = self._quellen(kurs)
        correlation_id = str(uuid4())
        eingabe = auftrag["input"]
        if auftrag["kind"] == "grobentwurf":
            entwurf = await grobentwurf_erzeugen(
                gateway,
                correlation_id=correlation_id,
                title=kurs["title"],
                audience=kurs["audience"],
                outcomes=kurs["outcomes"],
                default_kind=kurs["default_kind"],
                hinweise=list(eingabe.get("hinweise", [])),
                source_refs=katalog.model_refs,
            )
            # Everything the model said is validated; only now is the course touched.
            self._store.kurs_aendern(
                kurs["course_id"], title=entwurf.title, default_kind=entwurf.default_kind
            )
            task_ids = [
                self._store.aufgabe_anlegen(
                    kurs["course_id"], chapter_title=kapitel.title, summary=aufgabe.summary
                )["task_id"]
                for kapitel in entwurf.chapters
                for aufgabe in kapitel.tasks
            ]
            return {"task_ids": task_ids}
        return await self._ausformulieren(auftrag, kurs, katalog, gateway, correlation_id)

    async def _ausformulieren(
        self, auftrag: dict[str, Any], kurs: dict[str, Any], katalog: Any,
        gateway: ModelGateway, correlation_id: str,
    ) -> dict[str, Any]:
        task_id = auftrag["task_id"]
        eingabe = auftrag["input"]
        aufgabe = next((a for a in kurs["aufgaben"] if a["task_id"] == task_id), None)
        if aufgabe is None:
            raise _Fehler("aufgabe_unbekannt")
        vorherige = self._store.neueste(task_id)
        ergebnis = await aufgabe_ausformulieren(
            gateway,
            correlation_id=correlation_id,
            kurs=kurs_fuer_modell(kurs),
            summary=aufgabe["summary"],
            kind=eingabe.get("kind") or kurs["default_kind"],
            wunsch=eingabe.get("wunsch"),
            vorherige=vorherige["content"] if vorherige else None,
            source_refs=katalog.model_refs,
            sources=katalog.snapshots,
        )
        inhalt = {**ergebnis.inhalt, "zitate": ergebnis.zitate}
        try:
            fassung = self._store.fassung_anlegen(
                task_id,
                content=inhalt,
                based_on=eingabe.get("based_on"),
                author_ref=AUTOR_REF,
                reason=eingabe.get("wunsch") or "Vom Modell ausformuliert",
                art=fassungs_art(vorherige["content"] if vorherige else None, inhalt),
                findings=list(ergebnis.befunde),
            )
        except WerkstattKonflikt as konflikt:
            raise _Fehler("konflikt", {"neueste": konflikt.neueste}) from konflikt
        return {"fassung": fassung}


def kurs_fuer_modell(kurs: dict[str, Any]) -> dict[str, Any]:
    """The course facts the author prompt needs; no task list, no owner, no provenance."""
    return {
        "course_id": kurs["course_id"], "title": kurs["title"], "audience": kurs["audience"],
        "outcomes": list(kurs["outcomes"]),
    }
