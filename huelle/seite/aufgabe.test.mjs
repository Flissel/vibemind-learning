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
