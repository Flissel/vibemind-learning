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
    default: // fill_in, open_response, case, code
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
  const roh = String(text ?? "").replace(DOLLAR, (_m, a, b) => latex(a ?? b)).replace(/\\\$/g, "$");
  return roh.split(/\s+/).filter(Boolean).join(" ");
}

export function maskiere(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

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
      html += maskiere(quelle.slice(ende, m.index).replace(/\\\$/g, "$"));
      const anzeige = m[1] !== undefined;
      html += katex.renderToString(anzeige ? m[1] : m[2], { displayMode: anzeige, throwOnError: true });
      ende = m.index + m[0].length;
    }
    html += maskiere(quelle.slice(ende).replace(/\\\$/g, "$"));
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

// ---- KI-Sperre ---------------------------------------------------------------------------------
export const SPERR_HINWEIS = "Während der Aufgabe hilft „Erklär mir das“.";

/** Der Hinweis zur KI-Sperre, solange eine Aufgabe offen ist; sonst null. */
export function sperrHinweis(turn) {
  return turn && turn.state === "active" && turn.task ? SPERR_HINWEIS : null;
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
