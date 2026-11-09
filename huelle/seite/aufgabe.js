// Reine Funktionen der Lern-Huelle (ohne DOM): Antwortformate des Forks und Formelsatz.

/** Eine Zahl, wie Lernende sie tippen: Komma oder Punkt, `1e3` erlaubt; sonst null. */
export function parseZahl(eingabe) {
  const text = String(eingabe ?? "").trim().replace(/−/g, "-").replace(",", ".");
  if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(text)) return null;
  const wert = Number(text);
  return Number.isFinite(wert) ? wert : null;
}

/**
 * Die Antwort einer Aufgabe im Format des Forks (practiceModel.ts / TaskRenderer.tsx).
 * `eingabe`: { auswahl: string[], text: string, reihenfolge: string[], paare: object }.
 * Null, wenn nichts zu senden ist (ungueltige Zahl, Zeichenaufgabe).
 */
export function antwortAus(task, eingabe) {
  const e = eingabe ?? {};
  switch (task.type) {
    case "single_choice":
      return { choice_id: (e.auswahl ?? [])[0] ?? "" };
    case "multiple_choice":
      return { choice_ids: [...(e.auswahl ?? [])] };
    case "ordering":
      return { order: [...(e.reihenfolge ?? [])] };
    case "matching": {
      const links = Array.isArray(task.presentation?.left) ? task.presentation.left : [];
      const paare = {};
      for (const eintrag of links) {
        if (eintrag && typeof eintrag.id === "string") paare[eintrag.id] = String((e.paare ?? {})[eintrag.id] ?? "");
      }
      return { pairs: paare };
    }
    case "numeric": {
      const wert = parseZahl(e.text);
      return wert === null ? null : { value: wert };
    }
    case "penecho_canvas":
      return null;
    default: // fill_in, open, case, code
      return { text: String(e.text ?? "") };
  }
}

// ---- Klartext (Spiegel von services/werkstatt/formeltext.klartext) -----------------------
const DOLLAR = /\$\$([\s\S]+?)\$\$|(?<!\\)\$([^$]+?)(?<!\\)\$/g;
const FRAC = /\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/;
const SYMBOLE = {
  to: "→", rightarrow: "→", infty: "∞", le: "≤", leq: "≤", ge: "≥", geq: "≥",
  ne: "≠", neq: "≠", cdot: "·", pm: "±", Rightarrow: "⇒", Leftrightarrow: "⇔",
  times: "×", approx: "≈",
};
const HOCH = { 2: "²", 3: "³", 4: "⁴" };
const TIEF = "₀₁₂₃₄₅₆₇₈₉";

function bruchTeil(t) {
  t = t.trim();
  let tiefe = 0;
  let offen = false;
  for (const z of t) {
    tiefe += (z === "(") - (z === ")");
    if (tiefe === 0 && " +-*/".includes(z)) offen = true;
  }
  return offen ? `(${t})` : t;
}

function latex(formel) {
  formel = formel.replace(/\\mathbb\{R\}/g, "ℝ");
  formel = formel.replace(/\\mathbb\{([A-Za-z])\}/g, "$1");
  formel = formel.replace(/\\(?:left|right)(?![A-Za-z])\s*/g, "");
  while (FRAC.test(formel)) {
    formel = formel.replace(FRAC, (_m, a, b) => `${bruchTeil(a)}/${bruchTeil(b)}`);
  }
  formel = formel.replace(/\\sqrt\s*\{([^{}]*)\}/g, (_m, a) => `√${bruchTeil(a)}`);
  formel = formel.replace(/\\([A-Za-z]+)/g, (_m, name) => SYMBOLE[name] ?? name);
  formel = formel.replace(/\^\{?([234])\}?(?!\d)/g, (_m, z) => HOCH[z]);
  formel = formel.replace(/([A-Za-z])_\{?(\d+)\}?/g, (_m, b, z) => b + [...z].map((c) => TIEF[Number(c)]).join(""));
  return formel.replace(/[{}]/g, "");
}

/** `$`-Zeichen entfernen und LaTeX in lesbaren Text verwandeln; Leerraum wird zusammengezogen. */
export function klartext(text) {
  const roh = String(text ?? "").replace(DOLLAR, (_m, a, b) => latex(a ?? b)).replace(/\\\$/g, "$").replace(FETT, "$1");
  return roh.split(/\s+/).filter(Boolean).join(" ");
}

export function maskiere(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Der Tutor schreibt Markdown-Fettdruck (**…**); erst maskieren, dann nur dieses eine Muster zulassen.
const FETT = /\*\*([^*\n]+?)\*\*/g;
const maskiertMitFett = (text) => maskiere(text).replace(FETT, "<strong>$1</strong>");

/**
 * Text mit `$…$` / `$$…$$` als HTML (KaTeX). Wirft KaTeX oder fehlt es, kommt Klartext ohne `$`.
 * Alles ausserhalb der Formeln wird maskiert.
 */
export function formel(text, katex) {
  const quelle = String(text ?? "");
  try {
    if (!katex || typeof katex.renderToString !== "function") throw new Error("katex");
    let html = "";
    let ende = 0;
    for (const m of quelle.matchAll(DOLLAR)) {
      html += maskiertMitFett(quelle.slice(ende, m.index).replace(/\\\$/g, "$"));
      const anzeige = m[1] !== undefined;
      html += katex.renderToString(anzeige ? m[1] : m[2], { displayMode: anzeige, throwOnError: true });
      ende = m.index + m[0].length;
    }
    html += maskiertMitFett(quelle.slice(ende).replace(/\\\$/g, "$"));
    return { html };
  } catch {
    return { text: klartext(quelle) };
  }
}

// ---- Meldungen der Bruecke (alle festen deutschen Texte aus bruecke.py) ---------------------
export const ALLGEMEIN = "Das hat gerade nicht geklappt. Bitte versuchen Sie es noch einmal.";
export const FESTE_MELDUNGEN = new Set([
  "Learning ist gerade nicht erreichbar. Ihre Fläche bleibt erhalten.",
  "Diese Aufgabe wurde schon abgegeben. Ich lade den aktuellen Stand.",
  "Diese Abgabe wurde nicht angenommen. Bitte prüfen Sie Ihre Eingabe.",
  "Dieser Kurs hat noch keine freigegebenen Aufgaben.",
  "Ihre Abgabe wird noch geprüft. Sie zählt, sobald die Prüfung fertig ist.",
  "Die Fläche ist gerade nicht erreichbar.",
  "Kein Hinweis verfügbar.",
  "Im Prüfungsmodus gibt es keine Hinweise.",
  "Bitte wählen Sie eines der drei Gesichter.",
  "Bitte nennen Sie höchstens drei Stichworte.",
  "Diesen Kurs gibt es hier nicht.",
  "Ungültige Eingabe.",
  "Die aktuelle Aufgabe ist keine Zeichenaufgabe.",
  "Während der Aufgabe hilft „Erklär mir das“.",
  // Fläche zuerst (bruecke.py).
  "Diese Aufgabe beantworten Sie in der Seitenleiste.",
  "Bei dieser Aufgabe gibt es nichts Gelesenes zu korrigieren.",
  "Die Bewertung ist gerade nicht erreichbar. Bitte geben Sie gleich noch einmal ab.",
  "In der Hülle können Sie nur üben. Diese Sitzung lässt keine Vorbewertung zu.",
  "Diese Rückmeldung lässt sich nicht bestätigen. Bitte überarbeiten Sie Ihr Ergebnis oder tippen Sie es selbst ein.",
]);

/** Der Banner-Text zu einer Fehlerantwort: feste Texte der Bruecke, sonst der allgemeine Text. */
export function meldungFuer(daten) {
  const m = daten && typeof daten.meldung === "string" ? daten.meldung : "";
  return FESTE_MELDUNGEN.has(m) ? m : ALLGEMEIN;
}

/** Nach einem 409 mit Turn: hat der Turn eine offene Aufgabe, ruft die Seite `weiter` (Flaeche, Sperre). */
export function nach409(turn) {
  return turn && turn.state === "active" && turn.task ? "weiter" : "zeigen";
}

/** Kinder fuer replaceChildren: null/undefined fallen weg (sonst steht "null" als Text da). */
export function ohneLeere(...kinder) {
  return kinder.filter((kind) => kind != null);
}

// ---- KI-Sperre ---------------------------------------------------------------------------------
export const SPERR_HINWEIS = "Während der Aufgabe hilft „Erklär mir das“.";

/** Der Hinweis zur KI-Sperre, solange eine Pruefungsaufgabe offen ist (beim Ueben ist die KI frei); sonst null. */
export function sperrHinweis(turn) {
  return turn && turn.state === "active" && turn.task && turn.mode !== "training" ? SPERR_HINWEIS : null;
}

// ---- Bilder (wie practiceModel.ts im Fork, Quelle ist die eigene Medienroute der Bruecke) -------
const MEDIEN = /^\/lernen\/api\/medien\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(png|jpg|webp)$/;
const positiv = (n) => typeof n === "number" && Number.isFinite(n) && n > 0;

/** Ein Bild der Aufgabe `{src, width, height}`, nur von der Medienroute und mit Groesse; sonst null. */
export function bildAus(wert) {
  if (!wert || typeof wert !== "object") return null;
  const { image, width, height } = wert;
  if (typeof image !== "string" || !MEDIEN.test(image) || !positiv(width) || !positiv(height)) return null;
  return { src: image, width, height };
}

function kasten(wert) {
  if (!wert || typeof wert !== "object") return null;
  const { x, y, w, h } = wert;
  const endlich = (n) => typeof n === "number" && Number.isFinite(n) && n >= 0;
  return endlich(x) && endlich(y) && positiv(w) && positiv(h) ? { x, y, w, h } : null;
}

/** Das gezeichnete Fenster eines Lueckentexts: Bild, Eingabekasten und gespiegelte Kaesten; sonst null. */
export function fensterAus(presentation) {
  const bild = bildAus(presentation);
  const rect = kasten(presentation?.rect);
  if (!bild || !rect) return null;
  const more = Array.isArray(presentation.more) ? presentation.more.map(kasten).filter(Boolean) : [];
  return { ...bild, rect, more };
}

/** Lage eines Kastens in Prozent des Bildes (TaskRenderer.tsx `percent`). */
export function prozent(r, bild) {
  return {
    left: `${(r.x / bild.width) * 100}%`,
    top: `${(r.y / bild.height) * 100}%`,
    width: `${(r.w / bild.width) * 100}%`,
    height: `${(r.h / bild.height) * 100}%`,
    fontSize: `clamp(9px, ${((r.h / bild.width) * 100 * 0.72).toFixed(3)}cqw, 18px)`,
  };
}

// Begriffstitel aus dem Graphen sind oft Slugs (`ablauf-kurvendiskussion`): lesbar machen, sonst unveraendert.
export function begriffTitel(titel) {
  const t = String(titel ?? "");
  if (t.includes(" ") || !/^[a-z0-9äöüß]+([-_][a-z0-9äöüß]+)*$/.test(t)) return t;
  const s = t.replace(/[-_]/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Begriffe mit gleichem Anzeigetitel (ohne Gross-/Kleinschreibung) werden einer; hoehere Beherrschung gewinnt (null/fehlend am niedrigsten). */
export function begriffeZusammenfassen(begriffe) {
  const stufe = (b) => (typeof b?.beherrschung === "number" ? b.beherrschung : -1);
  const reihe = [];
  const platz = new Map();
  for (const b of Array.isArray(begriffe) ? begriffe : []) {
    const schluessel = begriffTitel(b?.titel).toLowerCase();
    if (!platz.has(schluessel)) { platz.set(schluessel, reihe.length); reihe.push(b); continue; }
    const i = platz.get(schluessel);
    if (stufe(b) > stufe(reihe[i])) reihe[i] = b;
  }
  return reihe;
}

// ---- Flaechen-Hinweis ----------------------------------------------------------------------------
export const FLAECHEN_HINWEIS =
  "Die Fläche ist noch nicht verbunden. Schalten Sie oben in der Fläche „MCP“ ein und tippen Sie dann auf „Fläche verbinden“.";

/** Ob der Hinweis zur nicht verbundenen Flaeche gezeigt wird: Antwort mit flaeche false und offene Aufgabe. */
export function flaechenHinweis(daten) {
  return !!daten && daten.flaeche === false && daten.turn?.state === "active" && !!daten.turn?.task;
}

// ---- Fläche zuerst: Etiketten und Korrektur (Spiegel von contracts/beschriftung.py) -----------------
// Die gemeinsame Fixture tests/huelle/etiketten_fixture.json prüft beide Seiten.
const BUCHSTABEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
const MAX_TEXT = 2000;
export const DETERMINISTISCH = new Set(["single_choice", "multiple_choice", "matching", "ordering", "numeric", "fill_in"]);
const FLAECHENTYPEN = new Set([...DETERMINISTISCH, "open", "case", "penecho_canvas"]);

function ids(eintraege) {
  return eintraege.map((e) => {
    const wert = e && typeof e === "object" ? e.id : e;
    if (typeof wert !== "string" || !wert) throw new Error("labelled entry has no id");
    return wert;
  });
}

function buchstaben(liste) {
  if (liste.length > BUCHSTABEN.length) throw new Error("too many entries to label (at most 26)");
  return Object.fromEntries(liste.map((id, i) => [BUCHSTABEN[i], id]));
}

/** Etiketten der Aufgabe in angezeigter Reihenfolge: `{optionen, links, rechts}`. Wirft bei mehr als 26 Einträgen. */
export function etiketten(task) {
  const optionen = buchstaben(ids(Array.isArray(task?.options) ? task.options : []));
  let links = {};
  let rechts = {};
  if (task?.type === "matching") {
    const l = task.presentation?.left;
    const r = task.presentation?.right;
    if (Array.isArray(l)) links = Object.fromEntries(ids(l).map((id, i) => [String(i + 1), id]));
    if (Array.isArray(r)) rechts = buchstaben(ids(r));
  }
  return { optionen, links, rechts };
}

function etikettListe(text) {
  const teile = String(text ?? "").split(/[\s,;]+/).filter(Boolean).map((t) => t.toUpperCase());
  return teile.length > 0 && new Set(teile).size === teile.length ? teile : null;
}

function aufloesen(liste, tabelle) {
  if (!liste || liste.some((e) => !Object.hasOwn(tabelle, e))) return null;
  return liste.map((e) => tabelle[e]);
}

/** Wie `_zahl` der Brücke: Unicode-Minus, Komma als Dezimaltrenner, Tausenderzeichen; kein Exponent. */
function zahlAus(wert) {
  let text = String(wert ?? "").replace(/[−–—]/g, "-").replace(/\s+/g, "");
  // "1.500": deutsch 1500, englisch 1,5. Wie `_zahl`: lieber null als eine falsche Zahl.
  if (/^[+-]?[1-9]\d{0,2}\.\d{3}$/.test(text)) return null;
  if (text.includes(",") && text.includes(".")) {
    const dezimal = text.lastIndexOf(",") > text.lastIndexOf(".") ? "," : ".";
    const gruppe = dezimal === "," ? "." : ",";
    text = text.split(gruppe).join("").replace(dezimal, ".");
  } else if ((text.match(/,/g) ?? []).length === 1) {
    text = text.replace(",", ".");
  }
  if (!/^[+-]?(\d+(\.\d*)?|\.\d+)$/.test(text)) return null;
  const zahl = Number(text);
  return Number.isFinite(zahl) ? zahl : null;
}

function paareAus(text, tabelle) {
  const paare = {};
  const teile = String(text ?? "").split(/[,;\n]+/).map((t) => t.trim()).filter(Boolean);
  if (teile.length === 0) return null;
  for (const teil of teile) {
    const m = /^(\S+?)\s*[-−–—:=>→]+\s*(\S+)$/.exec(teil);
    if (!m) return null;
    const links = tabelle.links[m[1]];
    const rechts = tabelle.rechts[m[2].toUpperCase()];
    if (links === undefined || rechts === undefined || Object.hasOwn(paare, links)) return null;
    paare[links] = rechts;
  }
  return paare;
}

/**
 * Die getippte Korrektur („B“, „A, C“, „C, A, B“, „1-B, 2-A“, Zahl, Text) als Antwort im Format der
 * deterministischen Bewertung; null, wenn nichts sicher zu lesen ist. Spiegel von `antwort_aus_etiketten`.
 */
export function korrekturAntwort(task, text) {
  if (!task || !DETERMINISTISCH.has(task.type)) return null;
  let t;
  try {
    t = etiketten(task);
  } catch {
    return null;
  }
  switch (task.type) {
    case "single_choice": {
      const aus = aufloesen(etikettListe(text), t.optionen);
      return aus && aus.length === 1 ? { choice_id: aus[0] } : null;
    }
    case "multiple_choice": {
      const aus = aufloesen(etikettListe(text), t.optionen);
      return aus ? { choice_ids: aus } : null;
    }
    case "ordering": {
      const aus = aufloesen(etikettListe(text), t.optionen);
      return aus ? { order: aus } : null;
    }
    case "matching": {
      const pairs = paareAus(text, t);
      return pairs ? { pairs } : null;
    }
    case "numeric": {
      const value = zahlAus(text);
      return value === null ? null : { value };
    }
    default: { // fill_in
      const roh = String(text ?? "");
      return roh.trim() && roh.length <= MAX_TEXT ? { text: roh.trim() } : null;
    }
  }
}

const EINGABE_HINWEIS = {
  single_choice: "Einen Buchstaben eingeben, z. B. B",
  multiple_choice: "Buchstaben eingeben, z. B. A, C",
  ordering: "Reihenfolge eingeben, z. B. C, A, B",
  matching: "Paare eingeben, z. B. 1-B, 2-A",
  numeric: "Zahl eingeben, z. B. -1,5",
  fill_in: "Text eingeben",
};

/** Der Hinweis zum Eingabefeld von „Falsch gelesen“ / „Selbst eintippen“; null für Typen ohne Korrektur. */
export function eingabeHinweis(typ) {
  return Object.hasOwn(EINGABE_HINWEIS, typ) ? EINGABE_HINWEIS[typ] : null;
}

// ---- Fläche zuerst: Leiste über der Fläche ----------------------------------------------------------
export const KNOPF_TEXT = {
  speichern: "Speichern", abgeben: "Abgeben", passt: "Passt", ueberarbeiten: "Überarbeiten",
  falsch: "Falsch gelesen", eintippen: "Selbst eintippen", neuBewerten: "Neu bewerten",
};
export const NICHT_LESBAR = "Nicht lesbar.";

/** Das Ergebnis einer Vorbewertung aus der Antwort von `abgeben`/`korrektur`; null ohne Vorbewertung. */
export function ergebnisAus(daten) {
  const v = daten?.vorbewertung;
  if (!v || typeof v.id !== "string" || typeof daten.urteil !== "string") return null;
  const ergebnis = { vorbewertung_id: v.id, urteil: daten.urteil, lesbar: v.lesbar !== false };
  // Karte nicht auf der Fläche (z. B. kein Platz): die Brücke schickt die Rückmeldung als Text für die Leiste.
  if (typeof daten.karte === "string" && daten.karte) ergebnis.karte = daten.karte;
  return ergebnis;
}

const unlesbar = (e) => e.lesbar === false || e.urteil === NICHT_LESBAR;

/**
 * Welche Knöpfe die Leiste zeigt. Ohne Ergebnis: Speichern, Abgeben. Mit lesbarem Urteil (auch „Wird noch
 * geprüft.“) zusätzlich Passt, Überarbeiten und bei deterministischen Typen Falsch gelesen; nicht lesbar:
 * Selbst eintippen statt Passt. `code` hat keine Leiste (Antwortformular in der Seitenleiste).
 */
export function leistenZustand(task, ergebnis) {
  if (!task || !FLAECHENTYPEN.has(task.type)) return { sichtbar: false, knoepfe: [] };
  const knoepfe = [];
  if (ergebnis) {
    const deterministisch = DETERMINISTISCH.has(task.type);
    if (unlesbar(ergebnis)) {
      knoepfe.push("ueberarbeiten");
      if (deterministisch) knoepfe.push("eintippen");
    } else {
      knoepfe.push("passt", "ueberarbeiten");
      if (deterministisch) knoepfe.push("falsch");
    }
  }
  knoepfe.push("speichern", "abgeben");
  return { sichtbar: true, knoepfe };
}

/**
 * Die Knöpfe der Leiste mit Text und Sperre, dazu eine Kennung. Die Seite baut die Leiste nur neu, wenn sich
 * die Kennung ändert: die Abfrage alle 2 s ersetzte sonst die Knöpfe unter dem Finger, und Klicks auf
 * „Selbst eintippen“ gingen ins Leere (Abnahme A2, Befund 8).
 */
export function leistenKnoepfe(ids, { busy, speichert, abgebenGesperrt }) {
  const knoepfe = ids.map((id) => ({
    id,
    haupt: id === "abgeben",
    disabled: Boolean(busy) || (id === "speichern" && Boolean(speichert)) || (id === "abgeben" && Boolean(abgebenGesperrt)),
    text: KNOPF_TEXT[id],
  }));
  return { knoepfe, kennung: JSON.stringify(knoepfe) };
}

/** Der Hinweis nach einem nicht lesbaren Ergebnis (ein unverändert erneut abgegebenes Bild bleibt nicht lesbar). */
export function ergebnisHinweis(ergebnis) {
  if (!ergebnis) return null;
  if (!unlesbar(ergebnis)) return ergebnis.karte ?? null;
  return "Nicht lesbar. Schreiben Sie die Antwort neu und drücken Sie „Abgeben“, oder wählen Sie „Selbst eintippen“. "
    + "Unverändert abgegeben bleibt sie nicht lesbar.";
}

// ---- Fläche zuerst: Speichern ----------------------------------------------------------------------------
/**
 * Speichert über `speichern()` (= `flaecheSpeichern(iframe)`); entschieden wird nur nach `code`:
 * bei "geaendert" genau eine Wiederholung, "leer" meldet nichts zu speichern. Löst immer auf:
 * `{ok, art}` mit art "ok" | "leer" | "fehler".
 */
export async function speichernMitWiederholung(speichern) {
  const einmal = async () => {
    try {
      return (await speichern()) ?? { ok: false, code: "fehler" };
    } catch {
      return { ok: false, code: "fehler" };
    }
  };
  let r = await einmal();
  if (!r.ok && r.code === "geaendert") r = await einmal();
  if (r.ok) return { ok: true, art: "ok" };
  return { ok: false, art: r.code === "leer" ? "leer" : "fehler" };
}

export const NICHT_GESPEICHERT = "Nicht gespeichert – bitte „Speichern“ drücken.";
export const NICHTS_ZU_SPEICHERN = "Auf der Fläche gibt es noch nichts zu speichern.";

/** Der ruhige Hinweis zum Speichern; null bei Erfolg. „Leer“ meldet nur der Knopf selbst (`manuell`). */
export function speicherHinweis(ergebnis, manuell) {
  if (ergebnis.ok) return null;
  if (ergebnis.art === "leer") return manuell ? NICHTS_ZU_SPEICHERN : null;
  return NICHT_GESPEICHERT;
}

export const GESPEICHERT = "Gespeichert.";

/**
 * Was ein fertiges Speichern in der Leiste ändert. `alt`: der bisherige Dauerhinweis „nicht gespeichert“.
 * Gehört das Ergebnis zu einem anderen Kurs (`token` ≠ `aktuell`), wird es verworfen. Sonst:
 * ok löscht den Dauerhinweis; ein Fehler setzt ihn (er bleibt über „Weiter“ stehen, bis ein Speichern klappt);
 * „leer“ meldet nur der Knopf selbst. `meldung` ist ein kurzer Hinweis für den Knopf, sonst null.
 */
export function speicherAusgang(ergebnis, { manuell, token, aktuell, alt = null }) {
  if (token !== aktuell) return { speicher: alt, meldung: null };
  if (ergebnis.ok) return { speicher: null, meldung: manuell ? GESPEICHERT : null };
  if (ergebnis.art === "leer") return { speicher: alt, meldung: manuell ? NICHTS_ZU_SPEICHERN : null };
  return { speicher: NICHT_GESPEICHERT, meldung: null };
}

const SCHON_ABGEGEBEN = "Diese Aufgabe wurde schon abgegeben. Ich lade den aktuellen Stand.";
export const VERALTET = "Die Karte war veraltet. Ich habe den aktuellen Stand geladen.";

/** Der Hinweis zu einer Fehlerantwort von abgeben/korrektur/passt; `veraltet`: Stand aus `turn` neu zeigen. */
export function fehlerText(r) {
  // Nur die Meldung „schon abgegeben“ heisst: Karte veraltet. Andere Ablehnungen zeigen ihre eigene Meldung.
  if (r?.status === 409 && r.daten?.turn && r.daten.meldung === SCHON_ABGEGEBEN) return { text: VERALTET, veraltet: true };
  return { text: meldungFuer(r?.daten), veraltet: false };
}
