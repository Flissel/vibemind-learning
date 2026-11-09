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
KERNAUSSAGE_ZEICHEN = 900
KURS_KURZ_ZEICHEN = 40

_SEITE = (8.27, 11.69)  # A4 hoch, Zoll
_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


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
    aufgaben: tuple[AufgabeZeile, ...]
    begriffe: tuple[tuple[str, str], ...]


def kurs_kurz(titel: str) -> str:
    """Kleingeschrieben, Umlaute umgeschrieben, sonst nur [a-z0-9-], hoechstens 40 Zeichen; leer -> „kurs“."""
    kurz = re.sub(r"[^a-z0-9]+", "-", titel.lower().translate(_UMLAUTE)).strip("-")[:KURS_KURZ_ZEICHEN].strip("-")
    return kurz or "kurs"


def _einzeilig(text: object) -> str:
    return " ".join(str(text).split())


def ergebnis_von(status: str, vorbewertung: dict[str, Any] | None) -> str:
    """Das Gelesene der Vorbewertung; eingetippt (ohne Gelesenes) „eingetippt“, Handschrift ohne Gelesenes
    (Rubrik) „siehe Bild“, ohne Vorbewertung „eingegeben“/„offen“.

    Den getippten Text liefert Learnings Vorbewertung nicht mit; „siehe Bild“ waere dafuer falsch (A2, Befund 6).
    """
    if isinstance(vorbewertung, dict):
        gelesen = vorbewertung.get("gelesen_text")
        if isinstance(gelesen, str) and gelesen.strip():
            return _einzeilig(gelesen)
        return "eingetippt" if vorbewertung.get("quelle") == "getippt" else "siehe Bild"
    return "eingegeben" if status == "bestaetigt" else "offen"


def kernaussage(vorbewertung: dict[str, Any]) -> str:
    """Urteil plus die ganze Rueckmeldung der Karte (alle Kriterien), hoechstens 900 Zeichen (sonst mit „…“)."""
    from .bruecke import kartentext, urteil_aus  # bruecke importiert dieses Modul

    ganz = f"{urteil_aus(vorbewertung)} {_einzeilig(kartentext(vorbewertung))}".strip()
    if len(ganz) <= KERNAUSSAGE_ZEICHEN:
        return ganz
    return ganz[:KERNAUSSAGE_ZEICHEN - 1].rstrip() + "…"


# ---- Begriffe ------------------------------------------------------------------------------------
_SLUG = re.compile(r"^[a-z0-9äöüß]+([-_][a-z0-9äöüß]+)*$")


# Slug -> deutscher Titel. Gleiche Regeln in seite/aufgabe.js (`begriffTitel`); beide pruefen dieselbe
# Fall-Liste tests/huelle/begriff_titel_faelle.json.
_VOKALE = "aeiouäöü"
_SZ_ANFANG = (("ausser", "außer"), ("groess", "größ"), ("gross", "groß"), ("schliess", "schließ"))
_KLEINWOERTER = frozenset({
    "der", "die", "das", "des", "dem", "den", "ein", "eine", "einer", "eines", "einem", "einen", "im", "in",
    "an", "am", "auf", "aus", "mit", "von", "vom", "zum", "zur", "und", "oder", "bei", "beim", "für", "über",
    "ohne", "nach", "außerhalb", "innerhalb", "unter", "zwischen", "als", "bis", "gegen", "pro", "je",
})
_ADJEKTIV = re.compile(r"(ig|lich|isch|bar|al|iv|ell|ant|ent|end|ar|ief|sam|haft|los)(e|er|es|en|em)$")
_KEIN_VERB = re.compile(r"(ionen|ungen|heiten|keiten)$")


def _umlaute(wort: str) -> str:
    """ae/oe/ue -> ä/ö/ü, ausser nach Vokal (neue, Feuer), nach q, vor ll/nt/nz (aktuell, kongruent) und in
    „koeff“ (Koeffizient). Wortanfaenge wie „ausser“/„groess“ bekommen ihr ß."""
    for alt, neu in _SZ_ANFANG:
        if wort.startswith(alt):
            wort = neu + wort[len(alt):]
            break
    aus: list[str] = []
    i = 0
    while i < len(wort):
        paar, vor, nach = wort[i:i + 2], wort[i - 1] if i else "", wort[i + 2:i + 4]
        if paar in ("ae", "oe", "ue") and not (vor and vor in _VOKALE):
            behalten = (paar == "ue" and (vor == "q" or nach in ("ll", "nt", "nz"))) or \
                       (paar == "oe" and vor == "k" and nach == "ff")
            if not behalten:
                aus.append({"ae": "ä", "oe": "ö", "ue": "ü"}[paar])
                i += 2
                continue
        aus.append(wort[i])
        i += 1
    return "".join(aus)


def begriff_titel(titel: object) -> str:
    """Slugs aus dem Graphen (`hebbare-definitionsluecke`) als deutschen Titel („Hebbare Definitionslücke“),
    sonst unveraendert. Substantive gross; Adjektive vor einem Wort, ein Verb am Ende und kleine Woerter
    („im“, „des“) klein; ein vokalloses erstes Wort ist ein Formelzeichen („y-Achsenabschnitt“, „pq-Formel“)."""
    t = str(titel if titel is not None else "")
    if " " in t or not _SLUG.fullmatch(t):
        return t
    w = [_umlaute(teil) for teil in re.split(r"[-_]", t)]
    n = len(w)

    def adjektiv(i: int) -> bool:
        return i < n - 1 and w[i + 1] not in _KLEINWOERTER and bool(_ADJEKTIV.search(w[i]))

    def gross(s: str) -> str:
        return s[:1].upper() + s[1:]

    zeichen = n > 1 and not any(c in _VOKALE for c in w[0])
    aus = []
    for i, wort in enumerate(w):
        if i == 0:
            aus.append(wort if zeichen else gross(wort))
        elif wort in _KLEINWOERTER or adjektiv(i):
            aus.append(wort)
        elif (i == n - 1 and wort.endswith("en") and not _KEIN_VERB.search(wort)
              and w[i - 1] not in _KLEINWOERTER and not adjektiv(i - 1)):
            aus.append(wort)  # Verb am Ende: „Ableitungen bilden“
        else:
            aus.append(gross(wort))
    return f"{aus[0]}-{' '.join(aus[1:])}" if zeichen else " ".join(aus)


def begriffe_zusammenfassen(begriffe: list[Any]) -> list[dict[str, Any]]:
    """Gleicher Anzeigetitel (ohne Gross-/Kleinschreibung) wird ein Begriff mit lesbarem Titel; die hoehere
    Beherrschung gewinnt (fehlend am niedrigsten). Wie `begriffeZusammenfassen` in aufgabe.js."""
    def stufe(b: dict[str, Any]) -> float:
        wert = b.get("beherrschung")
        return float(wert) if isinstance(wert, (int, float)) and not isinstance(wert, bool) else -1.0

    reihe: list[dict[str, Any]] = []
    platz: dict[str, int] = {}
    for b in begriffe:
        if not isinstance(b, dict):
            continue
        b = {**b, "titel": begriff_titel(b.get("titel"))}
        schluessel = b["titel"].lower()
        if schluessel not in platz:
            platz[schluessel] = len(reihe)
            reihe.append(b)
        elif stufe(b) > stufe(reihe[platz[schluessel]]):
            reihe[platz[schluessel]] = {**b, "titel": reihe[platz[schluessel]]["titel"]}
    return reihe


# ---- Formeln als Klartext (PDF) -----------------------------------------------------------------
# matplotlib setzt kein volles LaTeX; ein unbekannter Befehl liesse das ganze PDF scheitern. Darum werden
# Formeln zwischen `$…$`/`$$…$$` in lesbaren Text mit Unicode-Zeichen umgeschrieben (A2, Befund 6).
_FORMEL = re.compile(r"\$\$(.+?)\$\$|(?<!\\)\$(?!\s)([^$\n]+?)(?<![\s\\])\$")
_HOCH = str.maketrans("0123456789+-=()niax", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃˣ")
_TIEF = str.maketrans("0123456789+-=()aeiox", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢₒₓ")
_ZEICHEN = {
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "infty": "∞", "to": "→",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftrightarrow": "⇔", "implies": "⇒",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "in": "∈",
    "notin": "∉", "subset": "⊂", "cup": "∪", "cap": "∩", "emptyset": "∅", "forall": "∀", "exists": "∃",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ", "epsilon": "ε", "varepsilon": "ε",
    "lambda": "λ", "mu": "μ", "pi": "π", "sigma": "σ", "Sigma": "Σ", "phi": "φ", "varphi": "φ", "omega": "ω",
    "sum": "∑", "int": "∫", "prime": "′", "degree": "°", "circ": "°", "ldots": "…", "dots": "…", "cdots": "⋯",
}
_DOPPELSTRICH = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ"}
_GRUPPE = r"\{([^{}]*)\}"


def _geklammert(teil: str) -> str:
    return teil if re.fullmatch(r"[\w′²³]+|\S", teil) else f"({teil})"


def _hoch_tief(teil: str, tabelle: dict[int, str], zeichen: str) -> str:
    """Hoch-/tiefgestellt, wenn jedes Zeichen eine Unicode-Form hat; sonst `^(…)`/`_(…)`."""
    if teil and all(ord(c) in tabelle for c in teil):
        return teil.translate(tabelle)
    return f"{zeichen}{_geklammert(teil)}"


def _formel_klartext(f: str) -> str:
    f = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)\s*([()\[\]|.])", lambda m: "" if m.group(1) == "." else m.group(1), f)
    f = re.sub(r"\\(?:left|right)\\([{}])", r"\1", f)
    f = re.sub(r"\\[,;:!]|\\quad|\\qquad|~", " ", f)
    for _ in range(8):  # innen nach aussen, damit verschachtelte Brueche und Wurzeln aufgehen
        vorher = f
        f = re.sub(r"\\(?:text|mathrm|mathit|mathbf|operatorname)\s*" + _GRUPPE, r"\1", f)
        f = re.sub(r"\\mathbb\s*\{?([A-Z])\}?", lambda m: _DOPPELSTRICH.get(m.group(1), m.group(1)), f)
        f = re.sub(r"\\[dt]?frac\s*" + _GRUPPE + r"\s*" + _GRUPPE,
                   lambda m: f"{_geklammert(m.group(1).strip())}/{_geklammert(m.group(2).strip())}", f)
        f = re.sub(r"\\sqrt\s*" + _GRUPPE, lambda m: "√" + _geklammert(m.group(1).strip()), f)
        f = re.sub(r"\^" + _GRUPPE, lambda m: _hoch_tief(m.group(1).strip(), _HOCH, "^"), f)
        f = re.sub(r"_" + _GRUPPE, lambda m: _hoch_tief(m.group(1).strip(), _TIEF, "_"), f)
        if f == vorher:
            break
    f = re.sub(r"\^(\w)", lambda m: _hoch_tief(m.group(1), _HOCH, "^"), f)
    f = re.sub(r"_(\w)", lambda m: _hoch_tief(m.group(1), _TIEF, "_"), f)
    f = re.sub(r"\\([A-Za-z]+)", lambda m: _ZEICHEN.get(m.group(1), m.group(1)), f)
    return f.replace("{", "").replace("}", "")


def ohne_latex(text: str) -> str:
    """Formeln in `text` als lesbarer Klartext; Text ausserhalb bleibt, `\\$` wird zu `$`."""
    ersetzt = _FORMEL.sub(lambda m: _formel_klartext(m.group(1) if m.group(1) is not None else m.group(2)), text)
    return ersetzt.replace("\\$", "$")


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
        zeilen = textwrap.wrap(_einzeilig(ohne_latex(text)), width=breite or self.BREITE) or [""]
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


def _urteil_kurz(kernaussage: str) -> str:
    """Der erste Satz der Rueckmeldung („Noch nicht richtig.“); „–“ (noch keine Rueckmeldung) wird leer."""
    k = _einzeilig(kernaussage)
    if not k or k == "–":
        return ""
    return re.split(r"(?<=[.!?])\s", k, maxsplit=1)[0]


def _titelseiten(z: Zusammenfassung) -> list[Figure]:
    """Kurs, Datum und eine Uebersicht je Aufgabe (Ergebnis, Urteil). Ein Bild der ganzen Flaeche gibt es nicht
    mehr: bei vielen Aufgaben war es ein unlesbarer Streifen (live 09.11.: 74 x 888 Pixel fuer 16 Aufgaben);
    jede Aufgabe hat ihr eigenes Bild auf ihrer Seite."""
    seiten: list[Figure] = []
    s = _Seite()
    s.text(z.kurs, groesse=20, fett=True, breite=44, max_zeilen=3)
    s.text(f"Zusammenfassung vom {_datum(z.datum)}", groesse=12, farbe="#555555")
    s.luft()
    s.text("Übersicht", groesse=11, fett=True)
    for a in z.aufgaben:
        if s.y < 0.07:
            seiten.append(s.fig)
            s = _Seite()
            s.text("Übersicht (Fortsetzung)", groesse=11, fett=True)
        urteil = _urteil_kurz(a.kernaussage)
        s.text(f"Aufgabe {a.nummer}: {a.ergebnis}" + (f" – {urteil}" if urteil else ""), max_zeilen=2)
    seiten.append(s.fig)
    return seiten


def _aufgabenseite(a: AufgabeZeile) -> Figure:
    s = _Seite()
    s.text(f"Aufgabe {a.nummer}", groesse=18, fett=True)
    s.luft()
    s.text(f"Aufgabe: {a.text}", max_zeilen=9)
    s.text(f"Ergebnis: {a.ergebnis}", max_zeilen=3)
    s.text(f"Rückmeldung: {a.kernaussage}", max_zeilen=12)
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
    """A4 hoch: Seite 1 Kurs, Datum, Uebersicht; je Aufgabe eine Seite; zum Schluss „Begriffe“."""
    puffer = io.BytesIO()
    # fonttype 42: die Schrift (DejaVu Sans, liegt matplotlib bei) steckt als TrueType im PDF, der Text bleibt lesbar.
    with matplotlib.rc_context({"font.family": "DejaVu Sans", "pdf.fonttype": 42}):
        with PdfPages(puffer) as pdf:
            for fig in [*_titelseiten(z), *(_aufgabenseite(a) for a in z.aufgaben), *_begriffeseiten(z)]:
                pdf.savefig(fig)
    return puffer.getvalue()
