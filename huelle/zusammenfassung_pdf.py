"""Das PDF der Zusammenfassung, gesetzt mit ReportLab.

Aufbau: Seite 1 sagt auf einen Blick, wie der Kurs steht (Zahlen, Balken, Tabelle aller Aufgaben). Danach
stehen nur die bearbeiteten Aufgaben ausfuehrlich da, als Karten, mehrere je Seite. Offene Aufgaben haben
keine eigene Seite, Begriffe ohne Wert keine eigene Zeile.

Anlass (21.11.2026): das alte PDF (matplotlib, eine Seite je Aufgabe) hatte bei 9 Aufgaben 11 Seiten, davon
6 fast leere mit „Kein Bild“, abgeschnittene Aufgabentexte und Bilder in beliebiger Groesse.

Rein wie `zusammenfassung.py`: nimmt eine fertige `Zusammenfassung`, liest nichts bei Learning oder PenEcho.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Flowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .zusammenfassung import (
    NICHT_BEARBEITET, AufgabeZeile, Zusammenfassung, _einzeilig, ki_genutzt, ohne_latex,
)

# Farben der Huelle (huelle.css), damit Seite und PDF zusammengehoeren.
TINTE = colors.HexColor("#17191c")
GRAU = colors.HexColor("#55524a")
LINIE = colors.HexColor("#c9c6bc")
PAPIER = colors.HexColor("#f6f5f1")
GRUEN, GRUEN_HELL = colors.HexColor("#1d7a3e"), colors.HexColor("#e7f3eb")
GELB, GELB_HELL = colors.HexColor("#8a5a00"), colors.HexColor("#fff6dc")
ROT, ROT_HELL = colors.HexColor("#8a1c13"), colors.HexColor("#fdecea")
OFFEN, OFFEN_HELL = colors.HexColor("#55524a"), colors.HexColor("#f0eee8")

RAND = 18 * mm
# Der Rahmen von ReportLab hat innen je 6 Punkt Luft; Tabellen in genau dieser Breite stehen buendig zum Text.
BREITE = A4[0] - 2 * RAND - 12
BILD_MAX_HOEHE = 78 * mm
# Ein Bildpunkt der Aufnahme wird hoechstens so gross: ein kleiner Ausschnitt (eine Zeile Schrift) fuellte
# sonst die halbe Seite in Riesenschrift.
PUNKT_JE_PIXEL = 0.42

_SCHRIFT, _FETT = "Zusammenfassung", "Zusammenfassung-Fett"

# Status einer Aufgabe: Wort, Schriftfarbe, Flaeche.
STATUS = {
    "richtig": ("Richtig", GRUEN, GRUEN_HELL),
    "teilweise": ("Teilweise richtig", GELB, GELB_HELL),
    "falsch": ("Noch nicht richtig", ROT, ROT_HELL),
    "leer": ("Nicht bearbeitet", OFFEN, OFFEN_HELL),
    "offen": ("Offen", OFFEN, OFFEN_HELL),
}


def _schriften() -> None:
    """DejaVu Sans (liegt matplotlib bei, das Learning ohnehin braucht): deckt Pfeile, Hoch- und Tiefstellung
    ab, die `ohne_latex` schreibt, und steckt als TrueType im PDF."""
    if _SCHRIFT in pdfmetrics.getRegisteredFontNames():
        return
    import matplotlib

    ordner = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    pdfmetrics.registerFont(TTFont(_SCHRIFT, str(ordner / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(_FETT, str(ordner / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily(_SCHRIFT, normal=_SCHRIFT, bold=_FETT, italic=_SCHRIFT, boldItalic=_FETT)


def _stil(name: str, **werte: Any) -> ParagraphStyle:
    vorgabe: dict[str, Any] = {"fontName": _SCHRIFT, "fontSize": 9.5, "leading": 13.5, "textColor": TINTE}
    return ParagraphStyle(name, **{**vorgabe, **werte})


TEXT = _stil("text")
PUNKT = _stil("punkt", leftIndent=10, bulletIndent=1)
KLEIN = _stil("klein", fontSize=8.2, leading=11.5, textColor=GRAU)
MARKE = _stil("marke", fontName=_FETT, fontSize=7.4, leading=10, textColor=GRAU)
TITEL = _stil("titel", fontName=_FETT, fontSize=22, leading=27)
UNTERTITEL = _stil("untertitel", fontSize=11, leading=15, textColor=GRAU)
ABSCHNITT = _stil("abschnitt", fontName=_FETT, fontSize=13, leading=17, spaceBefore=4, spaceAfter=5)
KARTENKOPF = _stil("kartenkopf", fontName=_FETT, fontSize=11.5, leading=15)
ZELLE = _stil("zelle", fontSize=8.4, leading=11.2)
ZAHL = _stil("zahl", fontName=_FETT, fontSize=24, leading=28, alignment=TA_CENTER)
ZAHL_TEXT = _stil("zahltext", fontSize=8.5, leading=11, alignment=TA_CENTER, textColor=GRAU)


def _x(text: object) -> str:
    """Klartext fuer einen ReportLab-Absatz: Formeln umgeschrieben, einzeilig, XML-Zeichen maskiert."""
    return escape(_einzeilig(ohne_latex(str(text))))


def status_von(a: AufgabeZeile) -> str:
    """„richtig“ | „teilweise“ | „falsch“ | „leer“ (gebucht ohne Abgabe) | „offen“."""
    if a.ergebnis == NICHT_BEARBEITET:
        return "leer"
    if a.urteil in ("richtig", "teilweise", "falsch"):
        return a.urteil
    k = _einzeilig(a.kernaussage)
    if a.richtig or k.startswith("Richtig"):
        return "richtig"
    if k.startswith("Teilweise"):
        return "teilweise"
    if not k or k == "–":
        return "offen"
    return "falsch"


def erster_satz(text: str) -> str:
    """Der erste Satz einer Aufgabenstellung, fuer die Tabelle: ein ganzer Satz, nie mitten im Wort abgeschnitten."""
    t = _einzeilig(ohne_latex(text))
    teile = re.split(r"(?<=[.!?:])\s", t, maxsplit=1)
    return teile[0] if teile and teile[0] else t


def rueckmeldung_teile(kernaussage: str) -> tuple[str, list[str]]:
    """(Einleitung, Punkte) der Rueckmeldung. Die Karte reiht ihre Kriterien mit „ – “ aneinander; im PDF
    stehen sie als Liste. Ohne solche Punkte ist alles Einleitung."""
    k = _einzeilig(ohne_latex(kernaussage))
    if not k or k == "–":
        return "", []
    teile = [t.strip() for t in re.split(r"\s–\s(?=[A-ZÄÖÜ„])", k) if t.strip()]
    if len(teile) < 3:
        return k, []
    return teile[0], teile[1:]


class _Pille(Flowable):
    """Status als abgerundete Marke."""

    def __init__(self, status: str) -> None:
        super().__init__()
        self.wort, self.farbe, self.flaeche = STATUS[status]
        self.width = pdfmetrics.stringWidth(self.wort, _FETT, 7.8) + 5 * mm
        self.height = 5.2 * mm

    def wrap(self, *_: float) -> tuple[float, float]:
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        c.setFillColor(self.flaeche)
        c.setStrokeColor(self.flaeche)
        c.roundRect(0, 0, self.width, self.height, self.height / 2, stroke=0, fill=1)
        c.setFillColor(self.farbe)
        c.setFont(_FETT, 7.8)
        c.drawCentredString(self.width / 2, 1.6 * mm, self.wort)


class _Balken(Flowable):
    """Anteile als ein Balken: richtig, teilweise, falsch, Rest offen."""

    def __init__(self, anteile: list[tuple[float, Any]], breite: float, hoehe: float = 3.2 * mm) -> None:
        super().__init__()
        self.anteile, self.width, self.height = anteile, breite, hoehe

    def wrap(self, *_: float) -> tuple[float, float]:
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        c.setFillColor(OFFEN_HELL)
        c.roundRect(0, 0, self.width, self.height, self.height / 2, stroke=0, fill=1)
        pfad = c.beginPath()
        pfad.roundRect(0, 0, self.width, self.height, self.height / 2)
        c.saveState()
        c.clipPath(pfad, stroke=0, fill=0)
        x = 0.0
        for anteil, farbe in self.anteile:
            c.setFillColor(farbe)
            c.rect(x, 0, self.width * anteil, self.height, stroke=0, fill=1)
            x += self.width * anteil
        c.restoreState()


def _bild(png: bytes | None, breite: float) -> Image | None:
    """Die Aufnahme des Canvas in ruhiger Groesse; ein unlesbares Bild faellt weg (die Karte bleibt)."""
    if not png:
        return None
    try:
        b, h = ImageReader(io.BytesIO(png)).getSize()
    except Exception:  # noqa: BLE001 - ein kaputtes Bild darf das PDF nicht verhindern
        return None
    if b <= 0 or h <= 0:
        return None
    faktor = min(breite / b, BILD_MAX_HOEHE / h, PUNKT_JE_PIXEL)
    return Image(io.BytesIO(png), width=b * faktor, height=h * faktor)


def _kachel(zahl: int, wort: str, farbe: Any, flaeche: Any, breite: float) -> Table:
    t = Table([[Paragraph(f'<font color="{farbe.hexval()}">{zahl}</font>', ZAHL)], [Paragraph(escape(wort), ZAHL_TEXT)]],
              colWidths=[breite])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), flaeche), ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("TOPPADDING", (0, 0), (-1, 0), 8), ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
        ("TOPPADDING", (0, 1), (-1, 1), 0), ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
    ]))
    return t


def _kopfblock(z: Zusammenfassung, zaehler: dict[str, int]) -> list[Any]:
    gesamt = len(z.aufgaben)
    richtig, teilweise = zaehler["richtig"], zaehler["teilweise"]
    falsch, offen = zaehler["falsch"] + zaehler["leer"], zaehler["offen"]
    luecke = 4 * mm
    breite = (BREITE - 3 * luecke) / 4
    kacheln = Table([[
        _kachel(richtig, "richtig", GRUEN, GRUEN_HELL, breite), "",
        _kachel(teilweise, "teilweise richtig", GELB, GELB_HELL, breite), "",
        _kachel(falsch, "noch nicht richtig", ROT, ROT_HELL, breite), "",
        _kachel(offen, "noch offen", OFFEN, OFFEN_HELL, breite),
    ]], colWidths=[breite, luecke, breite, luecke, breite, luecke, breite])
    kacheln.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    anteile = [(n / gesamt, f) for n, f in ((richtig, GRUEN), (teilweise, GELB), (falsch, ROT))] if gesamt else []
    bearbeitet = gesamt - offen
    return [
        Paragraph(_x(z.kurs), TITEL),
        Spacer(1, 2 * mm),
        Paragraph(f"Zusammenfassung vom {z.datum:%d.%m.%Y}", UNTERTITEL),
        Spacer(1, 7 * mm),
        kacheln,
        Spacer(1, 4 * mm),
        _Balken(anteile, BREITE),
        Spacer(1, 2 * mm),
        Paragraph(f"{bearbeitet} von {gesamt} Aufgaben bearbeitet, {richtig} davon richtig.", KLEIN),
        Spacer(1, 7 * mm),
    ]


def _tabelle(z: Zusammenfassung) -> list[Any]:
    kopf = [Paragraph("NR.", MARKE), Paragraph("AUFGABE", MARKE), Paragraph("IHR ERGEBNIS", MARKE), Paragraph("STAND", MARKE)]
    zeilen: list[list[Any]] = [kopf]
    for a in z.aufgaben:
        s = status_von(a)
        ergebnis = "" if s == "offen" else _x(a.ergebnis)
        zeilen.append([Paragraph(str(a.nummer), ZELLE), Paragraph(escape(erster_satz(a.text)), ZELLE),
                       Paragraph(ergebnis, ZELLE), _Pille(s)])
    spalten = [9 * mm, BREITE - 9 * mm - 50 * mm - 36 * mm, 50 * mm, 36 * mm]
    t = Table(zeilen, colWidths=spalten, repeatRows=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, TINTE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINIE),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (0, -1), 0), ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
    ]))
    return [Paragraph("Alle Aufgaben", ABSCHNITT), t]


def _kursrueckmeldung(z: Zusammenfassung) -> list[Any]:
    if z.rueckmeldung is None:
        return []
    teile: list[Any] = [Spacer(1, 7 * mm), Paragraph("Kursrückmeldung", ABSCHNITT)]
    for titel, text in z.abschnitte():
        teile += [Paragraph(escape(titel.upper()), MARKE), Paragraph(_x(text), TEXT), Spacer(1, 3 * mm)]
    return teile


def _feld(marke: str, inhalt: list[Any]) -> list[Any]:
    return [Paragraph(marke, MARKE), *inhalt, Spacer(1, 2.6 * mm)]


def _karte(a: AufgabeZeile) -> Any:
    innen = BREITE - 10 * mm
    kopf = Table([[Paragraph(f"Aufgabe {a.nummer}", KARTENKOPF), _Pille(status_von(a))]],
                 colWidths=[innen - 40 * mm, 40 * mm])
    kopf.setStyle(TableStyle([("ALIGN", (1, 0), (1, 0), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    inhalt: list[Any] = [kopf, Spacer(1, 3 * mm)]
    inhalt += _feld("AUFGABE", _absaetze(_einzeilig(ohne_latex(a.text))))
    inhalt += _feld("IHR ERGEBNIS", _absaetze(_einzeilig(ohne_latex(a.ergebnis))))
    einleitung, punkte = rueckmeldung_teile(a.rueckmeldung or a.kernaussage)
    if einleitung:
        liste = _absaetze(einleitung)
        for p in punkte:
            liste += _absaetze(p, punkt=True)
        inhalt += _feld("RÜCKMELDUNG", liste)
    bild = _bild(a.bild, innen)
    if bild is not None:
        rahmen = Table([[bild]], colWidths=[innen])
        rahmen.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINIE), ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                                    ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        inhalt += _feld("IHRE FLÄCHE", [Spacer(1, 1 * mm), rahmen])
    fuss = " · ".join(t for t in (a.versuche_zeile, ki_genutzt(a.ki_schritte)) if t)
    inhalt.append(Paragraph(escape(fuss), KLEIN))
    # Je Baustein eine Tabellenzeile: so bricht eine Karte, die laenger als eine Seite ist, zwischen den
    # Bausteinen um. Als eine einzige Zelle liess sie sich gar nicht setzen (LayoutError).
    karte = Table([[baustein] for baustein in inhalt], colWidths=[BREITE])
    karte.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PAPIER), ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, 0), 4.5 * mm), ("BOTTOMPADDING", (0, -1), (-1, -1), 4.5 * mm),
    ]))
    return karte


# Ein Absatz in einer Tabellenzeile bricht nicht um; sehr lange Texte werden darum an Satzenden geteilt.
ABSATZ_ZEICHEN = 600


def _absaetze(text: str, *, punkt: bool = False) -> list[Any]:
    """`text` als ein oder mehrere Absaetze von hoechstens etwa `ABSATZ_ZEICHEN`, geteilt an Satzenden; mit
    `punkt` beginnt der erste mit einem Aufzaehlungspunkt, die uebrigen stehen eingerueckt darunter."""
    stuecke: list[str] = []
    for satz in re.split(r"(?<=[.!?])\s", text):
        if stuecke and len(stuecke[-1]) + len(satz) + 1 <= ABSATZ_ZEICHEN:
            stuecke[-1] += " " + satz
        else:
            stuecke.append(satz)
    stil = PUNKT if punkt else TEXT
    return [Paragraph(escape(s), stil, bulletText="•" if punkt and i == 0 else None) for i, s in enumerate(stuecke)]


def _karten(z: Zusammenfassung) -> list[Any]:
    bearbeitet = [a for a in z.aufgaben if status_von(a) != "offen"]
    if not bearbeitet:
        return []
    # Kein Seitenwechsel davor: die Karten schliessen an die Tabelle an. Die Ueberschrift bleibt bei der ersten.
    teile: list[Any] = [Spacer(1, 8 * mm)]
    kopf = [Paragraph("Ihre bearbeiteten Aufgaben", ABSCHNITT), Spacer(1, 1 * mm)]
    for i, a in enumerate(bearbeitet):
        teile += [KeepTogether([*(kopf if i == 0 else []), _karte(a)]), Spacer(1, 5 * mm)]
    return teile


def _begriffe(z: Zusammenfassung) -> list[Any]:
    """Nur Begriffe mit Wert stehen einzeln da, als Balken; die uebrigen als eine Zeile."""
    mit_wert: list[tuple[str, float]] = []
    for titel, stand in z.begriffe:
        treffer = re.fullmatch(r"\s*(\d{1,3})\s*%\s*", str(stand))
        if treffer:
            mit_wert.append((str(titel), min(100, int(treffer.group(1))) / 100))
    offen = len(z.begriffe) - len(mit_wert)
    teile: list[Any] = [Spacer(1, 4 * mm), Paragraph("Begriffe", ABSCHNITT)]
    if mit_wert:
        zeilen = [[Paragraph(_x(t), ZELLE), _Balken([(w, GRUEN)], 70 * mm, 2.6 * mm), Paragraph(f"{round(w * 100)} %", ZELLE)]
                  for t, w in mit_wert]
        t = Table(zeilen, colWidths=[BREITE - 70 * mm - 18 * mm - 6 * mm, 70 * mm + 6 * mm, 18 * mm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (0, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                               ("ALIGN", (2, 0), (2, -1), "RIGHT")]))
        teile.append(t)
    if offen:
        wort = "weiterer Begriff ist" if offen == 1 and mit_wert else "weitere Begriffe sind" if mit_wert else \
            ("Begriff ist" if offen == 1 else "Begriffe sind")
        teile += [Spacer(1, 2 * mm), Paragraph(
            f"{offen} {wort} noch offen: dazu ist noch keine Antwort gebucht.", KLEIN)]
    if not z.begriffe:
        teile.append(Paragraph("Noch keine Begriffe.", KLEIN))
    return [KeepTogether(teile)]


class _Zaehlend(Canvas):
    """Schreibt Kopf und „Seite x von y“ erst am Ende, wenn die Seitenzahl feststeht."""

    kurs = ""
    datum = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._seiten: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802 - Name von ReportLab
        self._seiten.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        gesamt = len(self._seiten)
        for nummer, zustand in enumerate(self._seiten, start=1):
            self.__dict__.update(zustand)
            self.setFont(_SCHRIFT, 7.6)
            self.setFillColor(GRAU)
            if nummer > 1:
                self.drawString(RAND, A4[1] - 11 * mm, self.kurs)
                self.setStrokeColor(LINIE)
                self.setLineWidth(0.4)
                self.line(RAND, A4[1] - 13 * mm, A4[0] - RAND, A4[1] - 13 * mm)
            self.drawString(RAND, 10 * mm, f"Zusammenfassung vom {self.datum}")
            self.drawRightString(A4[0] - RAND, 10 * mm, f"Seite {nummer} von {gesamt}")
            super().showPage()
        super().save()


def als_pdf(z: Zusammenfassung) -> bytes:
    """A4 hoch: Seite 1 Stand und Tabelle aller Aufgaben (dazu die Kursrueckmeldung, falls vorhanden); danach
    die bearbeiteten Aufgaben als Karten; zum Schluss die Begriffe mit Wert."""
    _schriften()
    zaehler = {name: 0 for name in STATUS}
    for a in z.aufgaben:
        zaehler[status_von(a)] += 1
    kurs = _einzeilig(ohne_latex(z.kurs))
    zeichner = type("_ZaehlendFuerKurs", (_Zaehlend,), {
        "kurs": kurs if len(kurs) <= 90 else kurs[:89].rstrip() + "…", "datum": f"{z.datum:%d.%m.%Y}"})
    puffer = io.BytesIO()
    doc = SimpleDocTemplate(puffer, pagesize=A4, leftMargin=RAND, rightMargin=RAND, topMargin=19 * mm,
                            bottomMargin=17 * mm, title=f"{kurs} – Zusammenfassung", author="Lern-Hülle")
    doc.build([*_kopfblock(z, zaehler), *_tabelle(z), *_kursrueckmeldung(z), *_karten(z), *_begriffe(z)],
              canvasmaker=zeichner)
    return puffer.getvalue()
