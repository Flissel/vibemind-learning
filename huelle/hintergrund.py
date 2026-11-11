"""Hintergrund-Spur: ein Arbeitsfaden, Vorrang vor Reihenfolge, keine doppelten Schluessel."""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Literal

log = logging.getLogger(__name__)


class _Keine:
    """Der gemeinsame Eimer aller Arbeiten ohne Person (`wer=None`): zaehlt als eine eigene Person."""


_KEINE = _Keine()


class Spur:
    """Ein Arbeitsfaden. Vorrang zuerst (kleinere Zahl zuerst); innerhalb eines Vorrangs reihum je Person
    (Reihenfolge der ersten Ankunft), innerhalb einer Person der Reihe nach."""

    def __init__(self, name: str, *, sperre: threading.Lock | None = None) -> None:
        self._name = name
        self._sperre = sperre
        # vorrang -> wer -> Wartende; die Reihenfolge der Personen (dict) ist die Reihum-Reihenfolge.
        self._wartende: dict[int, dict[object, deque[tuple[str, Callable[[], None]]]]] = {}
        self._zustaende: dict[str, Literal["wartet", "laeuft"]] = {}
        self._mutex = threading.Lock()
        self._wach = threading.Condition(self._mutex)
        self._offen = True
        self._faden = threading.Thread(target=self._lauf, name=f"spur-{name}", daemon=True)
        self._faden.start()

    def einreihen(
        self, schluessel: str, arbeit: Callable[[], None], *, vorrang: int = 1, wer: str | None = None,
    ) -> bool:
        with self._mutex:
            if not self._offen or schluessel in self._zustaende:
                return False
            self._zustaende[schluessel] = "wartet"
            personen = self._wartende.setdefault(vorrang, {})
            personen.setdefault(wer if wer is not None else _KEINE, deque()).append((schluessel, arbeit))
            self._wach.notify()
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
            for personen in self._wartende.values():
                for reihe in personen.values():
                    for schluessel, _ in reihe:
                        self._zustaende.pop(schluessel, None)
            self._wartende.clear()
            self._wach.notify_all()
        self._faden.join(timeout)

    def _naechste(self) -> tuple[str, Callable[[], None]] | None:
        """Unter `_mutex`: die naechste Arbeit nach Vorrang, dann reihum je Person."""
        if not self._wartende:
            return None
        vorrang = min(self._wartende)
        personen = self._wartende[vorrang]
        wer = next(iter(personen))
        reihe = personen.pop(wer)
        eintrag = reihe.popleft()
        if reihe:
            personen[wer] = reihe  # ans Ende: die naechste Person ist dran
        elif not personen:
            del self._wartende[vorrang]
        return eintrag

    def _lauf(self) -> None:
        while True:
            with self._mutex:
                while True:
                    if not self._offen:
                        return
                    eintrag = self._naechste()
                    if eintrag is not None:
                        break
                    self._wach.wait()
                schluessel, arbeit = eintrag
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
