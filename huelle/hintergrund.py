"""Hintergrund-Spur: ein Arbeitsfaden, Vorrang vor Reihenfolge, keine doppelten Schluessel."""

from __future__ import annotations

import itertools
import logging
import queue
import threading
from collections.abc import Callable
from typing import Literal

log = logging.getLogger(__name__)


class Spur:
    def __init__(self, name: str, *, sperre: threading.Lock | None = None) -> None:
        self._name = name
        self._sperre = sperre
        self._schlange: queue.PriorityQueue[tuple[int, int, str, Callable[[], None]]] = queue.PriorityQueue()
        self._zustaende: dict[str, Literal["wartet", "laeuft"]] = {}
        self._zaehler = itertools.count()
        self._mutex = threading.Lock()
        self._offen = True
        self._faden = threading.Thread(target=self._lauf, name=f"spur-{name}", daemon=True)
        self._faden.start()

    def einreihen(self, schluessel: str, arbeit: Callable[[], None], *, vorrang: int = 1) -> bool:
        with self._mutex:
            if not self._offen or schluessel in self._zustaende:
                return False
            self._zustaende[schluessel] = "wartet"
            self._schlange.put((vorrang, next(self._zaehler), schluessel, arbeit))
        return True

    def zustand(self, schluessel: str) -> Literal["wartet", "laeuft"] | None:
        with self._mutex:
            return self._zustaende.get(schluessel)

    def wartend(self, praefix: str) -> int:
        with self._mutex:
            return sum(1 for s in self._zustaende if s.startswith(praefix))

    def beenden(self, timeout: float = 5.0) -> None:
        with self._mutex:
            self._offen = False
            while True:
                try:
                    _, _, schluessel, _ = self._schlange.get_nowait()
                except queue.Empty:
                    break
                self._zustaende.pop(schluessel, None)
            # Aufwecker, damit der Faden aus dem blockierenden get() kommt.
            self._schlange.put((-1, next(self._zaehler), "", _nichts))
        self._faden.join(timeout)

    def _lauf(self) -> None:
        while True:
            _, _, schluessel, arbeit = self._schlange.get()
            if not schluessel:
                return
            with self._mutex:
                if schluessel not in self._zustaende:
                    continue
                self._zustaende[schluessel] = "laeuft"
            try:
                if self._sperre is not None:
                    with self._sperre:
                        arbeit()
                else:
                    arbeit()
            except Exception as fehler:  # noqa: BLE001 - Spur darf nie stehen bleiben
                log.warning("Spur %s: Arbeit fehlgeschlagen (%s)", self._name, type(fehler).__name__)
            finally:
                with self._mutex:
                    self._zustaende.pop(schluessel, None)


def _nichts() -> None:
    return None
