"""Zusammenfassung der Lern-Huelle: PDF, Textfassung, Kernaussage (rein, ohne Dienste)."""

from __future__ import annotations

import io
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pypdf

from spaces.learning.huelle.zusammenfassung import (
    KERNAUSSAGE_ZEICHEN, AufgabeZeile, Zusammenfassung, als_markdown, als_pdf, begriff_titel,
    begriffe_zusammenfassen, ergebnis_von, kernaussage, kurs_kurz, ohne_latex,
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


def test_titelseite_zeigt_uebersicht_statt_flaechenbild() -> None:
    # Live 09.11.: bei 16 Aufgaben war das Bild der ganzen Flaeche auf Seite 1 ein unlesbarer Streifen
    # (PenEcho liefert es mit 74 x 888 Pixeln). Seite 1 zeigt jetzt je Aufgabe Ergebnis und Urteil.
    pdf = als_pdf(_zusammenfassung())
    seite = _text(pdf)[0]
    assert "Übersicht" in seite
    assert "Aufgabe 1: -1,5 – Richtig." in seite
    assert "Aufgabe 2: siehe Bild – Noch nicht richtig." in seite
    assert "Gesamte Fläche" not in seite
    # matplotlib legt alle Bilder als gemeinsame Ressourcen ab; gezeichnet wird ein Bild per „/I… Do“.
    seiten = pypdf.PdfReader(io.BytesIO(pdf)).pages
    assert not re.search(rb"/I\d+ Do", seiten[0].get_contents().get_data())
    assert re.search(rb"/I\d+ Do", seiten[1].get_contents().get_data())


def test_titelseite_offene_aufgabe_und_viele_aufgaben() -> None:
    aufgaben = tuple(AufgabeZeile(n, f"Frage {n}", "offen", "–", None) for n in range(1, 61))
    seiten = _text(als_pdf(Zusammenfassung("Kurs", date(2026, 11, 9), aufgaben, ())))
    assert "Aufgabe 1: offen" in seiten[0] and "Aufgabe 1: offen –" not in seiten[0]
    # 60 Zeilen passen nicht auf eine Seite: die Uebersicht laeuft weiter, danach je Aufgabe eine Seite.
    ueberblick = [s for s in seiten if "Übersicht" in s]
    assert len(ueberblick) >= 2 and "Aufgabe 60: offen" in ueberblick[-1]
    assert seiten[:len(ueberblick)] == ueberblick
    assert len(seiten) == len(ueberblick) + 60 + 1


def test_pdf_ohne_bilder() -> None:
    pdf = als_pdf(_zusammenfassung(bilder=False))
    assert pdf.startswith(b"%PDF") and len(pypdf.PdfReader(io.BytesIO(pdf)).pages) == 4


def test_pdf_kaputtes_bild_und_dollar_zeichen() -> None:
    z = Zusammenfassung("Kurs $x$", date(2026, 11, 9),
                        (AufgabeZeile(1, "Lösen Sie $x^2$", "$5", "ok", b"\x89PNG kaputt"),), ())
    seiten = _text(als_pdf(z))
    assert len(seiten) == 3 and "x²" in seiten[1] and "$5" in seiten[1]


def test_markdown_format() -> None:
    z = Zusammenfassung("Kurs", date(2026, 11, 9),
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
        {"score": 0.5, "description": "Kriterium", "feedback": "a" * 1500}]}}
    k = kernaussage(vor)
    assert len(k) <= KERNAUSSAGE_ZEICHEN and k.endswith("…") and k.startswith("Teilweise richtig. ")


def test_kernaussage_ganze_rueckmeldung() -> None:
    vor = {"lesbar": True, "feedback": {"state": "correct", "criteria": []}}
    assert kernaussage(vor) == "Richtig. Gut gemacht."
    # Abnahme A2 (Befund 6): alle Kriterien, nicht nur der erste Satz des ersten.
    vor = {"lesbar": True, "feedback": {"state": "partial", "criteria": [
        {"score": 0.5, "description": "Kriterium A", "feedback": "Erster Satz. Zweiter Satz."},
        {"score": 0.0, "description": "Kriterium B", "feedback": "Dritter Satz."}]}}
    k = kernaussage(vor)
    assert k.startswith("Teilweise richtig. Kriterium A: Erster Satz. Zweiter Satz. Kriterium B: Dritter Satz. ")
    assert k.endswith("noch einmal abgeben. Die KI auf der Fläche erklärt Ihnen gern den Weg.")


def test_ergebnis_von() -> None:
    assert ergebnis_von("offen", {"gelesen_text": "7"}) == "7"
    assert ergebnis_von("offen", {"gelesen_text": None}) == "siehe Bild"
    assert ergebnis_von("bestaetigt", None) == "eingegeben"
    assert ergebnis_von("offen", None) == "offen"


def test_ergebnis_eingetippt_nie_siehe_bild() -> None:
    # Abnahme A2 (Befund 6): eine eingetippte Antwort hat kein Gelesenes, ist aber kein Bild.
    assert ergebnis_von("bestaetigt", {"quelle": "getippt", "gelesen_text": None}) == "eingetippt"
    assert ergebnis_von("offen", {"quelle": "handschrift", "gelesen_text": None}) == "siehe Bild"


AUSWAHL = {"type": "single_choice", "presentation": {},
           "options": [{"id": "a", "label": "Gerade"}, {"id": "b", "label": "Ungerade"}]}


def test_ergebnis_getippte_antwort_als_klartext() -> None:
    # Live 09.11.: bei getippten Antworten stand nur „eingetippt“; Learning liefert die Antwort jetzt mit.
    def getippt(antwort: dict[str, Any]) -> dict[str, Any]:
        return {"quelle": "getippt", "gelesen_text": None, "antwort": antwort}

    assert ergebnis_von("offen", getippt({"choice_id": "b"}), AUSWAHL) == "Ungerade"
    mehrfach = {**AUSWAHL, "type": "multiple_choice"}
    assert ergebnis_von("offen", getippt({"choice_ids": ["a", "b"]}), mehrfach) == "Gerade, Ungerade"
    reihe = {**AUSWAHL, "type": "ordering"}
    assert ergebnis_von("offen", getippt({"order": ["b", "a"]}), reihe) == "Ungerade → Gerade"
    zahl = {"type": "numeric", "options": [], "presentation": {"unit": "cm"}}
    assert ergebnis_von("bestaetigt", getippt({"value": -1.5}), zahl) == "-1,5 cm"
    assert ergebnis_von("bestaetigt", getippt({"value": 2.0}), {**zahl, "presentation": {}}) == "2"
    # Die Kursfabrik legt in `unit` auch Beschriftungen wie „x =“ ab (Live-Abnahme A3: „1 x =“): die stehen vorn.
    assert ergebnis_von("bestaetigt", getippt({"value": 1.0}), {**zahl, "presentation": {"unit": "x ="}}) == "x = 1"
    assert ergebnis_von("offen", getippt({"value": 2.5}),
                        {**zahl, "presentation": {"unit": "Steigung:"}}) == "Steigung: 2,5"
    offen = {"type": "open", "options": [], "presentation": {}}
    assert ergebnis_von("offen", getippt({"text": "  f'(x) = 3x²\n  + 1 "}), offen) == "f'(x) = 3x² + 1"
    paare = {"type": "matching", "options": [], "presentation": {
        "left": [{"id": "b1", "label": "x²"}, {"id": "b2", "label": "x³"}],
        "right": [{"id": "r1", "label": "gerade"}, {"id": "r2", "label": "ungerade"}]}}
    assert ergebnis_von("offen", getippt({"pairs": {"b1": "r1", "b2": "r2"}}), paare) == "x² → gerade; x³ → ungerade"


def test_ergebnis_getippt_ohne_verwertbare_antwort() -> None:
    # Unbekannte Kennungen bleiben stehen, leerer Text und fremde Formen ergeben „eingetippt“ wie bisher.
    def getippt(antwort: Any) -> dict[str, Any]:
        return {"quelle": "getippt", "gelesen_text": None, "antwort": antwort}

    assert ergebnis_von("offen", getippt({"choice_id": "z"}), AUSWAHL) == "z"
    assert ergebnis_von("offen", getippt({"text": "   "}), AUSWAHL) == "eingetippt"
    assert ergebnis_von("offen", getippt({"etwas": 1}), AUSWAHL) == "eingetippt"
    assert ergebnis_von("offen", getippt(None), AUSWAHL) == "eingetippt"
    assert ergebnis_von("offen", getippt({"value": True}), None) == "eingetippt"
    # Ohne Aufgabe (alte Aufrufer) bleibt es beim bisherigen Verhalten.
    assert ergebnis_von("offen", getippt({"choice_id": "b"})) == "b"


def test_ergebnis_gebucht_ohne_vorbewertung() -> None:
    zahl = {"type": "numeric", "options": [], "presentation": {}}
    assert ergebnis_von("bestaetigt", None, zahl, {"value": 7}) == "7"
    assert ergebnis_von("bestaetigt", None, zahl, None) == "eingegeben"
    assert ergebnis_von("offen", None, zahl, None) == "offen"


def test_formeln_als_lesbarer_text() -> None:
    assert ohne_latex("Lösen Sie $x^2 - 4 = 0$.") == "Lösen Sie x² - 4 = 0."
    assert ohne_latex(r"$f(x) = \frac{x^2-1}{x-2}$") == "f(x) = (x²-1)/(x-2)"
    assert ohne_latex(r"$\frac{1}{2}$") == "1/2"
    assert ohne_latex(r"$\lim_{x \to 2} f(x) = \infty$") == "lim_(x → 2) f(x) = ∞"
    assert ohne_latex(r"$\sqrt{x}$ und $\sqrt{x+1}$") == "√x und √(x+1)"
    assert ohne_latex(r"$x \cdot y \leq 3$, $x \in \mathbb{R}$") == "x · y ≤ 3, x ∈ ℝ"
    assert ohne_latex(r"$$a_1 = 5\text{ cm}$$") == "a₁ = 5 cm"
    assert ohne_latex(r"$\left( x \right)^{n+1}$") == "( x )ⁿ⁺¹"
    assert ohne_latex(r"Preis \$5 und $5") == "Preis $5 und $5"
    assert ohne_latex("Kein LaTeX hier.") == "Kein LaTeX hier."


def test_pdf_setzt_formeln_lesbar() -> None:
    z = Zusammenfassung("Kurs", date(2026, 11, 9),
                        (AufgabeZeile(1, r"Bestimmen Sie $\frac{1}{x}$ für $x \to \infty$.", "0", "ok", None),), ())
    seite = _text(als_pdf(z))[1]
    assert "1/x" in seite and "∞" in seite and "\\frac" not in seite and "$" not in seite


def test_begriffe_zusammenfassen() -> None:
    # Abnahme A2 (Befund 6): Dubletten aus Slugs mit `-` und `_`; hoehere Beherrschung gewinnt.
    begriffe = [
        {"titel": "ablauf-kurvendiskussion", "beherrschung": 0.2},
        {"titel": "ablauf_kurvendiskussion", "beherrschung": 0.6},
        {"titel": "Ableitung", "beherrschung": None},
        {"titel": "ableitung", "beherrschung": 0.4},
        {"titel": "Graph der Funktion"},
    ]
    assert begriffe_zusammenfassen(begriffe) == [
        {"titel": "Ablauf Kurvendiskussion", "beherrschung": 0.6},
        {"titel": "Ableitung", "beherrschung": 0.4},
        {"titel": "Graph der Funktion"},
    ]


def test_begriff_titel_gemeinsame_faelle() -> None:
    # Live 09.11.: „Hebbare definitionsluecke“ statt „Hebbare Definitionslücke“. Dieselbe Liste prueft
    # huelle/seite/aufgabe.test.mjs, damit PDF und Seitenleiste jeden Begriff gleich zeigen.
    faelle = json.loads((Path(__file__).parent / "begriff_titel_faelle.json").read_text(encoding="utf-8"))
    assert len(faelle) >= 30
    falsch = [(roh, begriff_titel(roh), soll) for roh, soll in faelle if begriff_titel(roh) != soll]
    assert falsch == []
    assert begriff_titel(None) == ""


def test_kurs_kurz() -> None:
    assert kurs_kurz("Mathe Übung: Größe & Maß!") == "mathe-uebung-groesse-mass"
    assert kurs_kurz("") == "kurs" and kurs_kurz("???") == "kurs"
    assert len(kurs_kurz("a" * 100)) == 40
    assert not kurs_kurz("ab " * 30).endswith("-")


def test_zusammenfassung_zeigt_ki_schritte() -> None:
    z = Zusammenfassung(
        kurs="Mathe", datum=date(2026, 11, 9),
        aufgaben=(
            AufgabeZeile(1, "A", "1", "ok", None, ("solve", "hint", "hint")),
            AufgabeZeile(2, "B", "2", "ok", None),
            AufgabeZeile(3, "C", "3", "ok", None, ("next_step", "explain", "check_step", "practice", "plot",
                                                  "answer", "frei", "abgeben", "next_step")),
        ),
        begriffe=(),
    )
    zeilen = [x for x in als_markdown(z).splitlines() if x.startswith("KI genutzt:")]
    assert zeilen == [
        "KI genutzt: Lösen, Hinweis ×2",
        "KI genutzt: –",
        "KI genutzt: Nächster Schritt ×2, Erklären, Prüfen, Übung, Zeichnen, Frage, Freie Anfrage, Abgabe",
    ]
