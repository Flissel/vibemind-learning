"""Zusammenfassung der Lern-Huelle: PDF, Textfassung, Kernaussage (rein, ohne Dienste)."""

from __future__ import annotations

import io
from datetime import date

import pypdf

from spaces.learning.huelle.zusammenfassung import (
    AufgabeZeile, Zusammenfassung, als_markdown, als_pdf, ergebnis_von, kernaussage, kurs_kurz,
)


def _png() -> bytes:
    from matplotlib.figure import Figure

    bild = Figure(figsize=(2, 1))
    bild.add_subplot().plot([0, 1], [0, 1])
    puffer = io.BytesIO()
    bild.savefig(puffer, format="png")
    return puffer.getvalue()


def _zusammenfassung(*, bilder: bool = True) -> Zusammenfassung:
    png = _png() if bilder else None
    return Zusammenfassung(
        kurs="Mathe Übung",
        datum=date(2026, 11, 9),
        flaeche=png,
        aufgaben=(
            AufgabeZeile(1, "Wie groß ist x?", "-1,5", "Richtig. Gut gemacht.", png),
            AufgabeZeile(2, "Zeichnen Sie die Gerade.", "siehe Bild", "Noch nicht richtig. Überarbeiten Sie Ihr Ergebnis.", png),
        ),
        begriffe=(("Gleichungen", "80 %"), ("Geraden", "noch offen")),
    )


def _text(pdf: bytes) -> list[str]:
    return [(s.extract_text() or "") for s in pypdf.PdfReader(io.BytesIO(pdf)).pages]


def test_pdf_hat_seiten_und_umlaute() -> None:
    seiten = _text(als_pdf(_zusammenfassung()))
    assert len(seiten) == 4
    assert "Aufgabe 1" in seiten[1] and "Aufgabe 2" in seiten[2]
    assert "Begriffe" in seiten[3]
    assert "Rückmeldung" in seiten[1]
    assert "Mathe Übung" in seiten[0] and "09.11.2026" in seiten[0]


def test_pdf_ohne_bilder() -> None:
    pdf = als_pdf(_zusammenfassung(bilder=False))
    assert pdf.startswith(b"%PDF") and len(pypdf.PdfReader(io.BytesIO(pdf)).pages) == 4


def test_pdf_kaputtes_bild_und_dollar_zeichen() -> None:
    z = Zusammenfassung("Kurs $x$", date(2026, 11, 9), b"kein bild",
                        (AufgabeZeile(1, "Lösen Sie $x^2$", "$5", "ok", b"\x89PNG kaputt"),), ())
    seiten = _text(als_pdf(z))
    assert len(seiten) == 3 and "$x^2$" in seiten[1]


def test_markdown_format() -> None:
    z = Zusammenfassung("Kurs", date(2026, 11, 9), b"png",
                        (AufgabeZeile(1, "Frage 1", "42", "Richtig. Gut gemacht.", b"png"),), (("Begriff", "80 %"),))
    text = als_markdown(z)
    zeilen = text.splitlines()
    assert zeilen[0] == "# Kurs – Zusammenfassung (09.11.2026)"
    assert "## Aufgabe 1" in zeilen
    i = zeilen.index("## Aufgabe 1")
    assert zeilen[i + 1:i + 4] == ["Aufgabe: Frage 1", "Ergebnis: 42", "Rückmeldung: Richtig. Gut gemacht."]
    assert zeilen[zeilen.index("## Begriffe") + 1] == "- Begriff: 80 %"
    assert "png" not in text and text.endswith("\n")


def test_kernaussage_gekuerzt() -> None:
    vor = {"lesbar": True, "feedback": {"state": "partial", "criteria": [
        {"score": 0.5, "description": "Kriterium", "feedback": "a" * 500}]}}
    k = kernaussage(vor)
    assert len(k) <= 300 and k.endswith("…") and k.startswith("Teilweise richtig. ")


def test_kernaussage_erster_satz() -> None:
    vor = {"lesbar": True, "feedback": {"state": "correct", "criteria": []}}
    assert kernaussage(vor) == "Richtig. Gut gemacht."
    vor = {"lesbar": True, "feedback": {"state": "partial", "criteria": [
        {"score": 0.5, "description": "Kriterium", "feedback": "Erster Satz. Zweiter Satz."}]}}
    assert kernaussage(vor) == "Teilweise richtig. Kriterium: Erster Satz."


def test_ergebnis_von() -> None:
    assert ergebnis_von("offen", {"gelesen_text": "7"}) == "7"
    assert ergebnis_von("offen", {"gelesen_text": None}) == "siehe Bild"
    assert ergebnis_von("bestaetigt", None) == "eingegeben"
    assert ergebnis_von("offen", None) == "offen"


def test_kurs_kurz() -> None:
    assert kurs_kurz("Mathe Übung: Größe & Maß!") == "mathe-uebung-groesse-mass"
    assert kurs_kurz("") == "kurs" and kurs_kurz("???") == "kurs"
    assert len(kurs_kurz("a" * 100)) == 40
    assert not kurs_kurz("ab " * 30).endswith("-")
