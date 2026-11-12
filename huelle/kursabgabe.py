"""„Kurs abgeben“ (ein Canvas je Aufgabe): Stand des Laufs und was mit jeder Aufgabe geschieht.

Rein: liest nichts bei Learning oder PenEcho. Den Lauf selbst fuehrt die Bruecke (`Bruecke._kurs_abgeben`), eine
Aufgabe je Arbeit der Modellspur.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

Zustand = Literal["laeuft", "fertig", "fehler"]
Fall = Literal["fertig", "bewerten", "leer"]

# Meldung im Stand `fehler`, wenn am Ende des Laufs Aufgaben nicht lesbar waren (feste Texte, `{liste}` aus
# `nummernliste`). Ein erneuter Lauf mit unveraenderter Aufnahme bucht sie als nicht bearbeitet.
UNLESBAR_EINE = (
    "Aufgabe {liste} konnte nicht gelesen werden. Bitte schreiben Sie deutlicher oder tippen Sie Ihre Antwort über "
    "„Falsch gelesen“ ein. Wenn Sie sie nicht bearbeiten möchten, drücken Sie noch einmal „Kurs abgeben“ – dann "
    "zählt sie als nicht bearbeitet."
)
UNLESBAR_MEHRERE = (
    "Aufgaben {liste} konnten nicht gelesen werden. Bitte schreiben Sie deutlicher oder tippen Sie Ihre Antworten "
    "über „Falsch gelesen“ ein. Wenn Sie sie nicht bearbeiten möchten, drücken Sie noch einmal „Kurs abgeben“ – dann "
    "zählen sie als nicht bearbeitet."
)


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


def nummernliste(nummern: Iterable[int]) -> str:
    """Aufsteigend: „4“, „4 und 6“, „2, 4 und 6“."""
    teile = [str(n) for n in sorted(nummern)]
    if len(teile) <= 1:
        return "".join(teile)
    return ", ".join(teile[:-1]) + " und " + teile[-1]


def unlesbar_text(nummern: Iterable[int]) -> str:
    """Fester Text zu den nicht lesbaren Aufgaben: Einzahl bei einer, sonst Mehrzahl."""
    liste = sorted(nummern)
    return (UNLESBAR_EINE if len(liste) == 1 else UNLESBAR_MEHRERE).format(liste=nummernliste(liste))
