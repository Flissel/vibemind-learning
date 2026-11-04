import test from "node:test";
import assert from "node:assert/strict";
import { antwortAus, formel, klartext, parseZahl } from "./aufgabe.js";

test("single_choice: choice_id", () => {
  assert.deepEqual(antwortAus({ type: "single_choice" }, { auswahl: ["b"] }), { choice_id: "b" });
  assert.deepEqual(antwortAus({ type: "single_choice" }, { auswahl: [] }), { choice_id: "" });
});

test("multiple_choice: choice_ids", () => {
  assert.deepEqual(antwortAus({ type: "multiple_choice" }, { auswahl: ["a", "c"] }), { choice_ids: ["a", "c"] });
});

test("numeric: value, Komma erlaubt, Unsinn null", () => {
  assert.deepEqual(antwortAus({ type: "numeric" }, { text: "-1,5" }), { value: -1.5 });
  assert.deepEqual(antwortAus({ type: "numeric" }, { text: " 1e3 " }), { value: 1000 });
  assert.equal(antwortAus({ type: "numeric" }, { text: "abc" }), null);
  assert.equal(parseZahl("−2"), -2);
});

test("ordering: order", () => {
  assert.deepEqual(antwortAus({ type: "ordering" }, { reihenfolge: ["x", "y"] }), { order: ["x", "y"] });
});

test("matching: pairs ueber alle linken Eintraege", () => {
  const task = { type: "matching", presentation: { left: [{ id: "l1", label: "A" }, { id: "l2", label: "B" }] } };
  assert.deepEqual(antwortAus(task, { paare: { l1: "r2" } }), { pairs: { l1: "r2", l2: "" } });
});

test("fill_in, open_response, case, code: text", () => {
  for (const type of ["fill_in", "open_response", "case", "code"]) {
    assert.deepEqual(antwortAus({ type }, { text: "abc" }), { text: "abc" });
  }
});

test("penecho_canvas: keine Antwort", () => {
  assert.equal(antwortAus({ type: "penecho_canvas" }, {}), null);
});

test("formel: wirft KaTeX, kommt Klartext ohne $", () => {
  const katex = { renderToString() { throw new Error("kaputt"); } };
  const r = formel("Es gilt $\\frac{1}{2} \\cdot x^2$ und $$\\sqrt{x}$$.", katex);
  assert.equal(r.html, undefined);
  assert.ok(!r.text.includes("$"));
  assert.equal(r.text, "Es gilt 1/2 · x² und √x.");
});

test("formel: fehlt KaTeX, kommt Klartext", () => {
  assert.deepEqual(formel("$x^2$", undefined), { text: "x²" });
});

test("formel: Erfolg maskiert den Rest", () => {
  const katex = { renderToString: (s, o) => `<k ${o.displayMode ? "d" : "i"}>${s}</k>` };
  assert.deepEqual(formel("<b>$a$</b> $$b$$", katex), { html: "&lt;b&gt;<k i>a</k>&lt;/b&gt; <k d>b</k>" });
});

test("formel: **fett** aus dem Tutor wird <strong>, ohne HTML durchzulassen", () => {
  const katex = { renderToString: (s) => `<k>${s}</k>` };
  assert.deepEqual(formel("**1. Schritt:** Bilden Sie $f'$. **<i>x</i>**", katex),
    { html: "<strong>1. Schritt:</strong> Bilden Sie <k>f'</k>. <strong>&lt;i&gt;x&lt;/i&gt;</strong>" });
});

test("klartext: **fett** ohne Sternchen", () => {
  assert.equal(klartext("**1. Definitionsbereich:** ganz $\\mathbb{R}$"), "1. Definitionsbereich: ganz ℝ");
});

test("klartext: Grundfaelle wie formeltext.py", () => {
  assert.equal(klartext("$\\frac{a+b}{c}$"), "(a+b)/c");
  assert.equal(klartext("Preis \\$5"), "Preis $5");
  assert.equal(klartext("$x_1 \\to \\infty$"), "x₁ → ∞");
});

import { meldungFuer, nach409, ALLGEMEIN, FESTE_MELDUNGEN } from "./aufgabe.js";

test("meldungFuer: alle festen deutschen Texte der Bruecke bleiben", () => {
  for (const m of [
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
  ]) {
    assert.ok(FESTE_MELDUNGEN.has(m), m);
    assert.equal(meldungFuer({ meldung: m }), m);
  }
});

test("meldungFuer: Fremdtexte werden allgemein", () => {
  assert.equal(meldungFuer({ meldung: "no task is open" }), ALLGEMEIN);
  assert.equal(meldungFuer({}), ALLGEMEIN);
  assert.equal(meldungFuer(null), ALLGEMEIN);
});

test("nach409: offene Aufgabe -> weiter, sonst zeigen", () => {
  assert.equal(nach409({ state: "active", task: { id: "t" } }), "weiter");
  assert.equal(nach409({ state: "completed", task: null }), "zeigen");
  assert.equal(nach409({ state: "active", task: null }), "zeigen");
  assert.equal(nach409(undefined), "zeigen");
});

import { bildAus, fensterAus, prozent, sperrHinweis, SPERR_HINWEIS } from "./aufgabe.js";

test("bildAus: nur Bilder der eigenen Medienroute mit Groesse", () => {
  assert.deepEqual(bildAus({ image: "/lernen/api/medien/graph-a.webp", width: 4, height: 3 }),
    { src: "/lernen/api/medien/graph-a.webp", width: 4, height: 3 });
  assert.equal(bildAus({ image: "https://evil.example/x.png", width: 4, height: 3 }), null);
  assert.equal(bildAus({ image: "/lernsequenz-medien/x.png", width: 4, height: 3 }), null);
  assert.equal(bildAus({ image: "/lernen/api/medien/../x.png", width: 4, height: 3 }), null);
  assert.equal(bildAus({ image: "/lernen/api/medien/x.png", width: 0, height: 3 }), null);
  assert.equal(bildAus({ image: "/lernen/api/medien/x.png" }), null);
  assert.equal(bildAus(null), null);
});

test("fensterAus: Lueckentext im gezeichneten Fenster nur mit Bild und Kasten", () => {
  const p = { image: "/lernen/api/medien/term-1.png", width: 200, height: 100,
    rect: { x: 10, y: 20, w: 50, h: 10 }, more: [{ x: 1, y: 2, w: 3, h: 4 }, { x: -1 }] };
  assert.deepEqual(fensterAus(p), { src: "/lernen/api/medien/term-1.png", width: 200, height: 100,
    rect: { x: 10, y: 20, w: 50, h: 10 }, more: [{ x: 1, y: 2, w: 3, h: 4 }] });
  assert.equal(fensterAus({ ...p, rect: undefined }), null);
  assert.equal(fensterAus({ ...p, image: "x.png" }), null);
});

test("prozent: Kasten relativ zum Bild wie im Fork", () => {
  const r = prozent({ x: 10, y: 20, w: 50, h: 10 }, { width: 200, height: 100 });
  assert.equal(r.left, "5%");
  assert.equal(r.top, "20%");
  assert.equal(r.width, "25%");
  assert.equal(r.height, "10%");
  assert.match(r.fontSize, /^clamp\(9px, [0-9.]+cqw, 18px\)$/);
});

test("sperrHinweis: bei offener Aufgabe der feste Text, sonst nichts", () => {
  assert.equal(SPERR_HINWEIS, "Während der Aufgabe hilft „Erklär mir das“.");
  assert.equal(sperrHinweis({ state: "active", task: { id: "t" } }), SPERR_HINWEIS);
  assert.equal(sperrHinweis({ state: "completed", task: null }), null);
  assert.equal(sperrHinweis(null), null);
});

import { begriffTitel } from "./aufgabe.js";

test("begriffTitel: Slug wird lesbar, alles andere bleibt", () => {
  assert.equal(begriffTitel("ablauf-kurvendiskussion"), "Ablauf kurvendiskussion");
  assert.equal(begriffTitel("ableitungsregeln"), "Ableitungsregeln");
  assert.equal(begriffTitel("größe-änderung"), "Größe änderung");
  assert.equal(begriffTitel("Ableitung der Sinusfunktion"), "Ableitung der Sinusfunktion");
  assert.equal(begriffTitel("Kettenregel"), "Kettenregel");
  assert.equal(begriffTitel("a--b"), "a--b");
  assert.equal(begriffTitel(""), "");
});

import { begriffeZusammenfassen, flaechenHinweis } from "./aufgabe.js";

test("begriffTitel: Unterstrich wie Bindestrich", () => {
  assert.equal(begriffTitel("hinreichende_bedingung"), "Hinreichende bedingung");
  assert.equal(begriffTitel("hinreichende-bedingung"), "Hinreichende bedingung");
  assert.equal(begriffTitel("a_b-c"), "A b c");
  assert.equal(begriffTitel("a__b"), "a__b");
});

test("begriffeZusammenfassen: gleiche Anzeigetitel werden eins, hoehere Beherrschung gewinnt", () => {
  const aus = begriffeZusammenfassen([
    { titel: "hinreichende_bedingung", beherrschung: 0.2 },
    { titel: "Kettenregel", beherrschung: null },
    { titel: "hinreichende-bedingung", beherrschung: 0.6 },
    { titel: "kettenregel" },
    { titel: "Produktregel", beherrschung: 0 },
    { titel: "produktregel", beherrschung: null },
  ]);
  assert.deepEqual(aus.map((b) => b.titel), ["hinreichende-bedingung", "Kettenregel", "Produktregel"]);
  assert.equal(aus[0].beherrschung, 0.6);
  assert.equal(aus[1].beherrschung ?? null, null);
  assert.equal(aus[2].beherrschung, 0);
  assert.deepEqual(begriffeZusammenfassen(null), []);
});

test("flaechenHinweis: nur bei flaeche false und offener Aufgabe", () => {
  const offen = { state: "active", task: { id: "t" } };
  assert.equal(flaechenHinweis({ flaeche: false, turn: offen }), true);
  assert.equal(flaechenHinweis({ flaeche: true, turn: offen }), false);
  assert.equal(flaechenHinweis({ turn: offen }), false);
  assert.equal(flaechenHinweis({ flaeche: false, turn: { state: "completed" } }), false);
  assert.equal(flaechenHinweis({ flaeche: false }), false);
  assert.equal(flaechenHinweis(null), false);
});
