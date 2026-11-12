"""„Kurs abgeben“ (ein Canvas je Aufgabe): Stand des Laufs und was mit jeder Aufgabe geschieht.

Rein: liest nichts bei Learning oder PenEcho. Den Lauf selbst fuehrt die Bruecke (`Bruecke._kurs_abgeben`), eine
Aufgabe je Arbeit der Modellspur.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Zustand = Literal["laeuft", "fertig", "fehler"]
Fall = Literal["fertig", "bewerten", "leer"]


@dataclass(frozen=True)
class KursAbgabeStand:
    """Stand eines Laufs fuer `/stand`: `aktuell` ist die Aufgabe, die gerade dran ist (am Ende `gesamt`).

    `meldung` nur bei `fehler`: ein Fehlercode der Bruecke (`FLAECHE_BESCHAEFTIGT`) oder ein fester Text.
    """

    zustand: Zustand
    aktuell: int
    gesamt: int
    meldung: str | None = None

    def als_json(self) -> dict[str, Any]:
        return {"zustand": self.zustand, "aktuell": self.aktuell, "gesamt": self.gesamt, "meldung": self.meldung}


def fall_fuer(status: str, hat_dokument: bool) -> Fall:
    """Bestaetigt: nichts mehr zu tun. Ohne Dokument (nie bearbeitet): leer buchen, 0 Punkte. Sonst aufnehmen und
    bewerten, auch eine offene, noch nicht bestaetigte Vorbewertung (die Flaeche kann sich seitdem geaendert haben)."""
    if status == "bestaetigt":
        return "fertig"
    if not hat_dokument:
        return "leer"
    return "bewerten"
