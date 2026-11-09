"""Zusammenfassung eines Listenkurses: PDF zum Herunterladen und Textfassung fuer den Wissensordner.

Rein: nimmt fertige Werte entgegen (`Zusammenfassung`), liest nichts bei Learning oder PenEcho. Die
Textfassung enthaelt weder Bilder noch Kennungen.
"""

from __future__ import annotations

import io
import re
import textwrap
from dataclasses import dataclass
from datetime import date
from typing import Any

import matplotlib

matplotlib.use("Agg")

from matplotlib import image as mpl_image  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

# Zeitbudget fuer alle Aufnahmen eines PDF-Abrufs zusammen; danach entfallen die uebrigen Bilder.
BILD_BUDGET_S = 30.0
KERNAUSSAGE_ZEICHEN = 300
KURS_KURZ_ZEICHEN = 40

_SEITE = (8.27, 11.69)  # A4 hoch, Zoll
_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_SATZ_ENDE = re.compile(r"(?<=[.!?])\s")


@dataclass(frozen=True)
class AufgabeZeile:
    nummer: int
    text: str
    ergebnis: str
    kernaussage: str
    bild: bytes | None


@dataclass(frozen=True)
class Zusammenfassung:
    kurs: str
    datum: date
    flaeche: bytes | None
    aufgaben: tuple[AufgabeZeile, ...]
    begriffe: tuple[tuple[str, str], ...]


def kurs_kurz(titel: str) -> str:
    """Kleingeschrieben, Umlaute umgeschrieben, sonst nur [a-z0-9-], hoechstens 40 Zeichen; leer -> „kurs“."""
    kurz = re.sub(r"[^a-z0-9]+", "-", titel.lower().translate(_UMLAUTE)).strip("-")[:KURS_KURZ_ZEICHEN].strip("-")
    return kurz or "kurs"


def _einzeilig(text: object) -> str:
    return " ".join(str(text).split())


def ergebnis_von(status: str, vorbewertung: dict[str, Any] | None) -> str:
    """Das Gelesene der Vorbewertung; ohne Gelesenes (Rubrik) „siehe Bild“, ohne Vorbewertung „eingegeben“/„offen“."""
    if isinstance(vorbewertung, dict):
        gelesen = vorbewertung.get("gelesen_text")
        return _einzeilig(gelesen) if isinstance(gelesen, str) and gelesen.strip() else "siehe Bild"
    return "eingegeben" if status == "bestaetigt" else "offen"


def kernaussage(vorbewertung: dict[str, Any]) -> str:
    """Urteil plus erster Satz der Rueckmeldung der Karte, hoechstens 300 Zeichen (sonst mit „…“ gekuerzt)."""
    from .bruecke import kartentext, urteil_aus  # bruecke importiert dieses Modul

    urteil = urteil_aus(vorbewertung)
    text = _einzeilig(kartentext(vorbewertung))
    satz = _SATZ_ENDE.split(text, maxsplit=1)[0] if text else ""
    ganz = f"{urteil} {satz}".strip()
    if len(ganz) <= KERNAUSSAGE_ZEICHEN:
        return ganz
    return ganz[:KERNAUSSAGE_ZEICHEN - 1].rstrip() + "…"


def _datum(d: date) -> str:
    return f"{d:%d.%m.%Y}"


def als_markdown(z: Zusammenfassung) -> str:
    zeilen = [f"# {_einzeilig(z.kurs)} – Zusammenfassung ({_datum(z.datum)})", ""]
    for a in z.aufgaben:
        zeilen += [
            f"## Aufgabe {a.nummer}",
            f"Aufgabe: {_einzeilig(a.text)}",
            f"Ergebnis: {_einzeilig(a.ergebnis)}",
            f"Rückmeldung: {_einzeilig(a.kernaussage)}",
            "",
        ]
    zeilen.append("## Begriffe")
    zeilen += [f"- {_einzeilig(titel)}: {_einzeilig(stand)}" for titel, stand in z.begriffe]
    return "\n".join(zeilen) + "\n"


# ---- PDF ---------------------------------------------------------------------------------------
def _bild_lesen(png: bytes | None) -> Any:
    if not png:
        return None
    try:
        return mpl_image.imread(io.BytesIO(png), format="png")
    except Exception:  # noqa: BLE001 - ein unlesbares Bild laesst die Seite nur ohne Bild
        return None


def _bild_zeigen(fig: Figure, png: bytes | None, rechteck: tuple[float, float, float, float]) -> None:
    daten = _bild_lesen(png)
    ax = fig.add_axes(rechteck)
    ax.axis("off")
    if daten is None:
        ax.text(0.5, 0.5, "Kein Bild", ha="center", va="center", color="#777777", parse_math=False)
        return
    ax.imshow(daten)  # aspect "equal": das Bild wird nie verzerrt


class _Seite:
    """Eine A4-Seite mit einem Textzeiger von oben nach unten."""

    ZEILE = 0.0215
    BREITE = 84

    def __init__(self) -> None:
        self.fig = Figure(figsize=_SEITE)
        self.y = 0.95

    def text(self, text: str, *, groesse: float = 10.5, fett: bool = False, breite: int | None = None,
             max_zeilen: int | None = None, farbe: str = "#1a1a1a") -> None:
        zeilen = textwrap.wrap(_einzeilig(text), width=breite or self.BREITE) or [""]
        if max_zeilen is not None and len(zeilen) > max_zeilen:
            zeilen = zeilen[:max_zeilen]
            zeilen[-1] = zeilen[-1][:max(0, self.BREITE - 2)].rstrip() + " …"
        for zeile in zeilen:
            self.fig.text(0.09, self.y, zeile, fontsize=groesse, fontweight="bold" if fett else "normal",
                          color=farbe, va="top", parse_math=False)
            self.y -= self.ZEILE * groesse / 10.5
        self.y -= 0.006

    def luft(self, menge: float = 0.015) -> None:
        self.y -= menge


def _titelseite(z: Zusammenfassung) -> Figure:
    s = _Seite()
    s.text(z.kurs, groesse=20, fett=True, breite=44, max_zeilen=3)
    s.text(f"Zusammenfassung vom {_datum(z.datum)}", groesse=12, farbe="#555555")
    s.luft()
    s.text("Gesamte Fläche", groesse=11, fett=True)
    unten = 0.06
    _bild_zeigen(s.fig, z.flaeche, (0.09, unten, 0.82, max(0.1, s.y - unten - 0.01)))
    return s.fig


def _aufgabenseite(a: AufgabeZeile) -> Figure:
    s = _Seite()
    s.text(f"Aufgabe {a.nummer}", groesse=18, fett=True)
    s.luft()
    s.text(f"Aufgabe: {a.text}", max_zeilen=9)
    s.text(f"Ergebnis: {a.ergebnis}", max_zeilen=3)
    s.text(f"Rückmeldung: {a.kernaussage}", max_zeilen=6)
    s.luft()
    unten = 0.06
    _bild_zeigen(s.fig, a.bild, (0.09, unten, 0.82, max(0.1, s.y - unten - 0.01)))
    return s.fig


def _begriffeseiten(z: Zusammenfassung) -> list[Figure]:
    seiten: list[Figure] = []
    s = _Seite()
    s.text("Begriffe", groesse=18, fett=True)
    s.luft()
    eintraege = [f"{titel}: {stand}" for titel, stand in z.begriffe] or ["Noch keine Begriffe."]
    for eintrag in eintraege:
        if s.y < 0.07:
            seiten.append(s.fig)
            s = _Seite()
            s.text("Begriffe (Fortsetzung)", groesse=14, fett=True)
        s.text(f"- {eintrag}", max_zeilen=3)
    seiten.append(s.fig)
    return seiten


def als_pdf(z: Zusammenfassung) -> bytes:
    """A4 hoch: Seite 1 Kurs, Datum, Flaechenbild; je Aufgabe eine Seite; zum Schluss „Begriffe“."""
    puffer = io.BytesIO()
    # fonttype 42: die Schrift (DejaVu Sans, liegt matplotlib bei) steckt als TrueType im PDF, der Text bleibt lesbar.
    with matplotlib.rc_context({"font.family": "DejaVu Sans", "pdf.fonttype": 42}):
        with PdfPages(puffer) as pdf:
            for fig in [_titelseite(z), *(_aufgabenseite(a) for a in z.aufgaben), *_begriffeseiten(z)]:
                pdf.savefig(fig)
    return puffer.getvalue()
