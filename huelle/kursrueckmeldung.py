"""Kursrueckmeldung am Kursende (ein Canvas je Aufgabe): Eingabe fuer die Flaechen-KI, ihre Antwort, Zahlen-Rueckfall.

Rein: liest nichts bei Learning, PenEcho oder der Lernflaeche. Die Eingabe enthaelt nur, was die Person ohnehin
sieht (Aufgabentext, ihr Ergebnis, die Rueckmeldung, KI-Schritte, Begriffe), nie Musterloesung oder Rubrik. Die
Lernflaeche prueft sie mit `exactKeys`; darum entsteht hier genau dieses Schema, schon auf die Grenzen gekuerzt.
Alle Grenzen zaehlen UTF-16-Codeeinheiten wie JavaScripts `.length` (`laenge`), nicht Python-Zeichen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .bericht import rein
from .zusammenfassung import KI_AKTIONEN, AufgabeZeile, Kursrueckmeldung

GRADER = "ki_flaeche"
KURS_ZEICHEN = 200
TITEL_ZEICHEN = 200
ERGEBNIS_ZEICHEN = 200
RUECKMELDUNG_ZEICHEN = 2000
SCHRITT_ZEICHEN = 40
SCHRITTE_HOECHSTENS = 50
AUFGABEN_HOECHSTENS = 100
BEGRIFFE_HOECHSTENS = 100
NAME_ZEICHEN = 200
STAND_ZEICHEN = 40
ANTWORT_ZEICHEN = 2000


def laenge(text: str) -> int:
    """Laenge wie JavaScripts `.length` (UTF-16-Codeeinheiten): so zaehlt der Pruefer der Lernflaeche. Ein Zeichen
    ausserhalb der BMP (Emoji, mathematische Buchstaben) zaehlt doppelt."""
    return len(text.encode("utf-16-le")) // 2


def _abschneiden(text: str, grenze: int) -> str:
    """Laengster Anfang mit hoechstens `grenze` UTF-16-Codeeinheiten; nie ein halbes Zeichen."""
    if laenge(text) <= grenze:
        return text
    teile: list[str] = []
    einheiten = 0
    for zeichen in text:
        breite = 2 if ord(zeichen) > 0xFFFF else 1
        if einheiten + breite > grenze:
            break
        teile.append(zeichen)
        einheiten += breite
    return "".join(teile)


def _kurz(text: object, grenze: int) -> str:
    """Einzeilig und hoechstens `grenze` UTF-16-Codeeinheiten (gekuerzt mit „…“, das eine Einheit zaehlt)."""
    t = " ".join(str(text).split())
    return t if laenge(t) <= grenze else _abschneiden(t, grenze - 1).rstrip() + "…"


def rueckmeldungsdaten(kurs: str, zeilen: Sequence[AufgabeZeile], begriffe: Sequence[Mapping[str, Any]]
                       ) -> dict[str, Any]:
    """Eingabe des Endpunkts `kursrueckmeldung`: `{kurs, aufgaben: [{nummer, titel, ergebnis, rueckmeldung,
    ki_schritte}], begriffe: [{name, stand}]}`, jede Liste und jeder Text auf die Grenzen des Schemas gekuerzt.

    `begriffe`: je Begriff `name` und `stand` (Strings); anderes faellt weg. Ohne Aufgaben ist `aufgaben` leer
    (der Endpunkt verlangt mindestens eine; der Aufrufer fragt dann nicht).
    """
    aufgaben = [{
        "nummer": int(z.nummer),
        "titel": _kurz(z.text, TITEL_ZEICHEN),
        "ergebnis": _kurz(z.ergebnis, ERGEBNIS_ZEICHEN),
        "rueckmeldung": _kurz(z.kernaussage, RUECKMELDUNG_ZEICHEN),
        "ki_schritte": [_kurz(KI_AKTIONEN.get(a, KI_AKTIONEN["frei"]), SCHRITT_ZEICHEN)
                        for a in z.ki_schritte[:SCHRITTE_HOECHSTENS]],
    } for z in zeilen if isinstance(z.nummer, int) and not isinstance(z.nummer, bool) and z.nummer >= 1]
    liste = [{"name": _kurz(b["name"], NAME_ZEICHEN), "stand": _kurz(b["stand"], STAND_ZEICHEN)}
             for b in begriffe
             if isinstance(b, Mapping) and isinstance(b.get("name"), str) and isinstance(b.get("stand"), str)]
    return {"kurs": _kurz(kurs, KURS_ZEICHEN), "aufgaben": aufgaben[:AUFGABEN_HOECHSTENS],
            "begriffe": liste[:BEGRIFFE_HOECHSTENS]}


def aus_antwort(wert: Mapping[str, Any] | None) -> Kursrueckmeldung | None:
    """Antwort der Flaechen-KI: nur mit `grader == "ki_flaeche"` und drei nicht leeren Texten, sonst None."""
    if not isinstance(wert, Mapping) or wert.get("grader") != GRADER:
        return None
    felder = [wert.get(k) for k in ("staerken", "luecken", "naechste_schritte")]
    if not all(isinstance(f, str) and f.strip() for f in felder):
        return None
    staerken, luecken, schritte = (_abschneiden(str(f).strip(), ANTWORT_ZEICHEN) for f in felder)
    return Kursrueckmeldung(staerken, luecken, schritte, "ki_flaeche")


def aus_zahlen(zeilen: Sequence[AufgabeZeile]) -> Kursrueckmeldung:
    """Rueckfall ohne Flaechen-KI: richtig gezaehlt, die uebrigen Aufgaben als offen genannt."""
    offen = [z.nummer for z in zeilen if not z.richtig]
    richtig = len(zeilen) - len(offen)
    return Kursrueckmeldung(
        f"{richtig} von {len(zeilen)} Aufgaben richtig.",
        f"Offen: Aufgabe {', '.join(str(n) for n in offen)}." if offen else "Keine.",
        "Wiederholen Sie die offenen Aufgaben." if offen else "Weiter so.",
        "zahlen",
    )


def als_text(k: Kursrueckmeldung) -> str:
    """Abschnitt der Berichtskarte (Markdown); fremder Text wird entschaerft."""
    return "\n\n".join([
        "## Kursrückmeldung",
        f"**Stärken:** {rein(k.staerken)}",
        f"**Lücken:** {rein(k.luecken)}",
        f"**Nächste Schritte:** {rein(k.naechste_schritte)}",
    ])
