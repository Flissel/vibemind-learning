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
