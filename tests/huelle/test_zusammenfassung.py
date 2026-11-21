"""Zusammenfassung der Lern-Huelle: PDF, Textfassung, Kernaussage (rein, ohne Dienste)."""

from __future__ import annotations

import io
import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pypdf

from spaces.learning.huelle.zusammenfassung import (
    KERNAUSSAGE_ZEICHEN, NICHT_BEARBEITET, NICHT_BEARBEITET_RUECKMELDUNG, AufgabeZeile, Kursrueckmeldung,
    Zusammenfassung, als_markdown, als_pdf, als_pdf_einfach, begriff_titel, begriffe_zusammenfassen, ergebnis_von, kernaussage,
    kurs_kurz, nicht_bearbeitet, ohne_latex,
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
    seiten = _text(als_pdf_einfach(_zusammenfassung()))
    assert len(seiten) == 4
    assert "Aufgabe 1" in seiten[1] and "Aufgabe 2" in seiten[2]
    assert "Begriffe" in seiten[3]
    assert "Rückmeldung" in seiten[1]
    assert "Mathe Übung" in seiten[0] and "09.11.2026" in seiten[0]


def test_titelseite_zeigt_uebersicht_statt_flaechenbild() -> None:
    # Live 09.11.: bei 16 Aufgaben war das Bild der ganzen Flaeche auf Seite 1 ein unlesbarer Streifen
    # (PenEcho liefert es mit 74 x 888 Pixeln). Seite 1 zeigt jetzt je Aufgabe Ergebnis und Urteil.
    pdf = als_pdf_einfach(_zusammenfassung())
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
    seiten = _text(als_pdf_einfach(Zusammenfassung("Kurs", date(2026, 11, 9), aufgaben, ())))
    assert "Aufgabe 1: offen" in seiten[0] and "Aufgabe 1: offen –" not in seiten[0]
    # 60 Zeilen passen nicht auf eine Seite: die Uebersicht laeuft weiter, danach je Aufgabe eine Seite.
    ueberblick = [s for s in seiten if "Übersicht" in s]
    assert len(ueberblick) >= 2 and "Aufgabe 60: offen" in ueberblick[-1]
    assert seiten[:len(ueberblick)] == ueberblick
    assert len(seiten) == len(ueberblick) + 60 + 1


def test_pdf_ohne_bilder() -> None:
    pdf = als_pdf_einfach(_zusammenfassung(bilder=False))
    assert pdf.startswith(b"%PDF") and len(pypdf.PdfReader(io.BytesIO(pdf)).pages) == 4


def test_pdf_kaputtes_bild_und_dollar_zeichen() -> None:
    z = Zusammenfassung("Kurs $x$", date(2026, 11, 9),
                        (AufgabeZeile(1, "Lösen Sie $x^2$", "$5", "ok", b"\x89PNG kaputt"),), ())
    seiten = _text(als_pdf_einfach(z))
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


_RUECKMELDUNG = Kursrueckmeldung("Brüche sicher gelöst.", "Geraden zeichnen.", "Üben Sie Steigungen.", "ki_flaeche")


def test_markdown_mit_kursrueckmeldung() -> None:
    ohne = _zusammenfassung(bilder=False)
    assert ohne.rueckmeldung is None
    assert "Kursrückmeldung" not in als_markdown(ohne)
    zeilen = als_markdown(replace(ohne, rueckmeldung=_RUECKMELDUNG)).splitlines()
    i = zeilen.index("## Kursrückmeldung")
    assert zeilen[i + 1:i + 9] == ["### Stärken", "Brüche sicher gelöst.", "", "### Lücken", "Geraden zeichnen.", "",
                                   "### Nächste Schritte", "Üben Sie Steigungen."]
    assert i < zeilen.index("## Aufgabe 1") and zeilen.index("## Begriffe") > i


def test_pdf_mit_kursrueckmeldung() -> None:
    ohne = _text(als_pdf_einfach(_zusammenfassung(bilder=False)))
    assert not any("Kursrückmeldung" in s for s in ohne)
    lang = Kursrueckmeldung("Stark " * 330, "Lücke " * 330, "Schritt " * 250, "zahlen")
    for k in (_RUECKMELDUNG, lang):
        seiten = _text(als_pdf_einfach(replace(_zusammenfassung(bilder=False), rueckmeldung=k)))
        assert len(seiten) > 4
        seite = next(s for s in seiten if "Kursrückmeldung" in s)
        assert "Stärken" in seite and "Lücken" in seite
        assert any("Nächste Schritte" in s for s in seiten)
        assert "Aufgabe 1" in seiten[-3] and "Begriffe" in seiten[-1]


def test_kernaussage_gekuerzt() -> None:
    vor = {"lesbar": True, "feedback": {"state": "partial", "criteria": [
        {"score": 0.5, "description": "Kriterium", "feedback": "a" * 1500}]}}
    k = kernaussage(vor)
    assert len(k) <= KERNAUSSAGE_ZEICHEN and k.endswith("…") and k.startswith("Teilweise richtig. ")
    # Fuer das PDF ungekuerzt: dort endete die Rueckmeldung sonst mitten im Satz (21.11.).
    ganz = kernaussage(vor, grenze=None)
    assert len(ganz) > 1500 and not ganz.endswith("…") and ganz.startswith(k[:-1])


def test_kernaussage_ganze_rueckmeldung() -> None:
    vor = {"lesbar": True, "feedback": {"state": "correct", "criteria": []}}
    assert kernaussage(vor) == "Richtig. Gut gemacht."
    # Abnahme A2 (Befund 6): alle Kriterien, nicht nur der erste Satz des ersten.
    vor = {"lesbar": True, "feedback": {"state": "partial", "criteria": [
        {"score": 0.5, "description": "Kriterium A", "feedback": "Erster Satz. Zweiter Satz."},
        {"score": 0.0, "description": "Kriterium B", "feedback": "Dritter Satz."}]}}
    k = kernaussage(vor)
    assert k.startswith("Teilweise richtig. Es fehlt noch: – Kriterium A: Erster Satz. Zweiter Satz. – Kriterium B: Dritter Satz. ")
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


def test_nicht_bearbeitet() -> None:
    # Abnahme 13.11. (F6): „Kurs abgeben“ bucht nie bearbeitete Aufgaben mit leerer Antwort. Sie hiessen
    # „eingegeben“, und die KI-Kursrueckmeldung lobte sie. Eine wiederholt abgegebene unlesbare Aufnahme ebenso.
    unlesbar = {"quelle": "handschrift", "lesbar": False, "gelesen_text": "kaum"}
    assert nicht_bearbeitet("bestaetigt", None, {})
    assert nicht_bearbeitet("bestaetigt", unlesbar, None)
    assert not nicht_bearbeitet("offen", None, None)
    assert not nicht_bearbeitet("offen", None, {})
    assert not nicht_bearbeitet("offen", unlesbar, None)  # noch nicht gebucht: offen, nicht „nicht bearbeitet“
    assert not nicht_bearbeitet("bestaetigt", None, {"value": 7})
    assert not nicht_bearbeitet("bestaetigt", None, None)
    assert not nicht_bearbeitet("bestaetigt", {"quelle": "getippt", "lesbar": True, "gelesen_text": None}, None)
    assert not nicht_bearbeitet("bestaetigt", {"quelle": "handschrift", "gelesen_text": "7"}, None)  # ohne `lesbar`
    zahl = {"type": "numeric", "options": [], "presentation": {}}
    assert ergebnis_von("bestaetigt", None, zahl, {}) == NICHT_BEARBEITET == "nicht bearbeitet"
    assert ergebnis_von("bestaetigt", unlesbar, zahl) == NICHT_BEARBEITET
    assert ergebnis_von("offen", unlesbar, zahl) == "kaum"
    assert NICHT_BEARBEITET_RUECKMELDUNG == "Nicht bearbeitet – 0 Punkte."


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
    seite = _text(als_pdf_einfach(z))[1]
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


def test_versuche_zeile_in_textfassung() -> None:
    from spaces.learning.huelle.zusammenfassung import AufgabeZeile

    a = AufgabeZeile(2, "Frage", "Antwort", "Richtig. Gut gemacht.", None, (), True, versuche=3, erstes_urteil="incorrect")
    assert a.versuche_zeile == "Versuche: 3, zuerst falsch"
    assert AufgabeZeile(1, "F", "A", "–", None, (), True, versuche=2, erstes_urteil="correct").versuche_zeile == "Versuche: 2"
    assert AufgabeZeile(1, "F", "A", "–", None).versuche_zeile == ""
    # Abnahme 15.11.: blieb es falsch, steht kein „zuerst falsch“ daneben.
    falsch = AufgabeZeile(3, "F", "A", "–", None, (), False, versuche=5, erstes_urteil="incorrect", urteil="falsch")
    assert falsch.versuche_zeile == "Versuche: 5"
    besser = AufgabeZeile(3, "F", "A", "–", None, (), False, versuche=2, erstes_urteil="incorrect", urteil="teilweise")
    assert besser.versuche_zeile == "Versuche: 2, zuerst falsch"


# ---- Das gesetzte PDF (ReportLab) -----------------------------------------------------------------------------
# Anlass 21.11.: das alte PDF hatte bei 9 Aufgaben 11 Seiten, 6 davon fast leer („Kein Bild“), abgeschnittene
# Aufgabentexte und Bilder in beliebiger Groesse. Die alte Fassung bleibt nur als Ersatz (`als_pdf_einfach`).
from spaces.learning.huelle import zusammenfassung_pdf as gesetzt  # noqa: E402


def _png_gross(breite: int, hoehe: int) -> bytes:
    from PIL import Image

    puffer = io.BytesIO()
    Image.new("RGB", (breite, hoehe), "white").save(puffer, format="PNG")
    return puffer.getvalue()


def _alles(pdf: bytes) -> str:
    return " ".join(" ".join(s.split()) for s in _text(pdf))


def test_gesetztes_pdf_seite_1_sagt_den_stand() -> None:
    seiten = _text(als_pdf(_zusammenfassung()))
    eins = " ".join(seiten[0].split())
    assert "Mathe Übung" in eins and "Zusammenfassung vom 09.11.2026" in eins
    assert "2 von 2 Aufgaben bearbeitet, 1 davon richtig." in eins
    assert "Alle Aufgaben" in eins and "Wie groß ist x?" in eins and "Noch nicht richtig" in eins
    assert f"Seite 1 von {len(seiten)}" in eins
    assert len(seiten) <= 2  # zwei kurze Aufgaben brauchen keine vier Seiten mehr


def test_gesetztes_pdf_offene_aufgaben_haben_keine_eigene_seite() -> None:
    aufgaben = tuple(AufgabeZeile(n, f"Frage {n}.", "offen", "–", None) for n in range(1, 61))
    pdf = als_pdf(Zusammenfassung("Kurs", date(2026, 11, 9), aufgaben, ()))
    seiten = _text(pdf)
    assert len(seiten) <= 4  # frueher 63
    text = _alles(pdf)
    assert "Frage 1." in text and "Frage 60." in text
    assert "0 von 60 Aufgaben bearbeitet" in text
    assert "Ihre bearbeiteten Aufgaben" not in text and "Kein Bild" not in text


def test_gesetztes_pdf_schneidet_keine_texte_ab() -> None:
    lang = " ".join(f"Satz{n} gehört zur Aufgabe." for n in range(1, 121))
    rueck = "Noch nicht richtig. " + " ".join(f"Hinweis{n} steht da." for n in range(1, 121))
    a = AufgabeZeile(1, lang, "42", rueck[:200] + "…", None, rueckmeldung=rueck)
    text = _alles(als_pdf(Zusammenfassung("Kurs", date(2026, 11, 9), (a,), ())))
    assert "Satz120 gehört zur Aufgabe." in text
    assert "Hinweis120 steht da." in text  # die ungekuerzte Rueckmeldung, nicht die gekuerzte Kernaussage


def test_rueckmeldung_teile_macht_aus_kriterien_eine_liste() -> None:
    k = ("Noch nicht richtig. Es fehlt noch: – Das Zentromer steht an der Einschnürung: der Pfeil endet links. "
         "– Kurzer Arm und langer Arm – beide ohne Pfeil. – Ein Telomer steht am Ende.")
    einleitung, punkte = gesetzt.rueckmeldung_teile(k)
    assert einleitung == "Noch nicht richtig. Es fehlt noch:"
    assert punkte == ["Das Zentromer steht an der Einschnürung: der Pfeil endet links.",
                      "Kurzer Arm und langer Arm – beide ohne Pfeil.", "Ein Telomer steht am Ende."]
    # Ohne Kriterienliste bleibt alles ein Absatz; ein einzelner Gedankenstrich macht noch keine Liste.
    assert gesetzt.rueckmeldung_teile("Richtig. Gut gemacht.") == ("Richtig. Gut gemacht.", [])
    assert gesetzt.rueckmeldung_teile("Nicht bearbeitet – 0 Punkte.") == ("Nicht bearbeitet – 0 Punkte.", [])
    assert gesetzt.rueckmeldung_teile("–") == ("", [])


def test_status_von() -> None:
    def a(ergebnis: str, kern: str, **mehr: Any) -> AufgabeZeile:
        return AufgabeZeile(1, "Frage", ergebnis, kern, None, **mehr)

    assert gesetzt.status_von(a("offen", "–")) == "offen"
    assert gesetzt.status_von(a("42", "Richtig. Gut gemacht.")) == "richtig"
    assert gesetzt.status_von(a("42", "Teilweise richtig. Fast.")) == "teilweise"
    assert gesetzt.status_von(a("42", "Noch nicht richtig. Weiter.")) == "falsch"
    assert gesetzt.status_von(a(NICHT_BEARBEITET, NICHT_BEARBEITET_RUECKMELDUNG)) == "leer"
    # Das gebuchte Urteil geht vor dem Wortlaut der Karte.
    assert gesetzt.status_von(a("42", "Noch nicht richtig.", urteil="teilweise")) == "teilweise"
    assert gesetzt.status_von(a("42", "ok", richtig=True)) == "richtig"


def test_erster_satz_endet_am_satzende() -> None:
    assert gesetzt.erster_satz("Ordnen Sie zu. Begründen Sie mit einer Zahl.") == "Ordnen Sie zu."
    assert gesetzt.erster_satz("Ohne Punkt") == "Ohne Punkt"
    assert gesetzt.erster_satz("Bestimmen Sie $x^2$ für x = 2. Dann weiter.") == "Bestimmen Sie x² für x = 2."


def test_gesetztes_pdf_begriffe_nur_mit_wert_einzeln() -> None:
    begriffe = (("Gleichungen", "80 %"), ("Geraden", "noch offen"), ("Parabeln", "noch offen"))
    text = _alles(als_pdf(replace(_zusammenfassung(bilder=False), begriffe=begriffe)))
    assert "Gleichungen" in text and "80 %" in text
    assert "Geraden" not in text and "Parabeln" not in text
    assert "2 weitere Begriffe sind noch offen" in text
    ohne = _alles(als_pdf(replace(_zusammenfassung(bilder=False), begriffe=(("Geraden", "noch offen"),))))
    assert "1 Begriff ist noch offen" in ohne
    assert "Noch keine Begriffe." in _alles(als_pdf(replace(_zusammenfassung(bilder=False), begriffe=())))


def test_gesetztes_pdf_kaputtes_bild_formeln_und_dollar() -> None:
    z = Zusammenfassung("Kurs $x$", date(2026, 11, 9),
                        (AufgabeZeile(1, r"Lösen Sie $x^2$ & <b>mehr</b>", "$5", "ok", b"\x89PNG kaputt"),), ())
    text = _alles(als_pdf(z))
    assert "x²" in text and "$5" in text and "<b>mehr</b>" in text and "\\frac" not in text


def test_gesetztes_pdf_mit_kursrueckmeldung_und_bildern() -> None:
    pdf = als_pdf(replace(_zusammenfassung(), rueckmeldung=_RUECKMELDUNG))
    text = _alles(pdf)
    assert "Kursrückmeldung" in text and "STÄRKEN" in text and "Brüche sicher gelöst." in text
    assert "Üben Sie Steigungen." in text
    bilder = sum(len(list(s.images)) for s in pypdf.PdfReader(io.BytesIO(pdf)).pages)
    assert bilder == 2  # je bearbeiteter Aufgabe das Bild ihrer Flaeche


def test_bild_bleibt_in_ruhiger_groesse() -> None:
    # 21.11.: eine einzelne Zeile Schrift fuellte die halbe Seite; ein grosser Ausschnitt darf sie nicht sprengen.
    klein = gesetzt._bild(_png_gross(679, 283), gesetzt.BREITE)
    assert klein is not None and klein.drawWidth <= 679 * gesetzt.PUNKT_JE_PIXEL + 0.01
    gross = gesetzt._bild(_png_gross(4000, 4000), gesetzt.BREITE)
    assert gross is not None and gross.drawHeight <= gesetzt.BILD_MAX_HOEHE + 0.01
    breit = gesetzt._bild(_png_gross(6000, 300), gesetzt.BREITE)
    assert breit is not None and breit.drawWidth <= gesetzt.BREITE + 0.01
    assert gesetzt._bild(b"kein bild", gesetzt.BREITE) is None and gesetzt._bild(None, gesetzt.BREITE) is None


def test_ohne_reportlab_entsteht_die_einfache_fassung(monkeypatch: Any) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "spaces.learning.huelle.zusammenfassung_pdf", None)  # Import scheitert
    seiten = _text(als_pdf(_zusammenfassung(bilder=False)))
    assert len(seiten) == 4 and "Übersicht" in seiten[0]  # die alte Fassung: je Aufgabe eine Seite