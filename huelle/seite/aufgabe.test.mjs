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

test("fill_in, open, case, code: text", () => {
  for (const type of ["fill_in", "open", "case", "code"]) {
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
  assert.equal(sperrHinweis({ state: "active", mode: "exam", task: { id: "t" } }), SPERR_HINWEIS);
  // Ohne Modus wie die Bruecke: zaehlt als Pruefung.
  assert.equal(sperrHinweis({ state: "active", task: { id: "t" } }), SPERR_HINWEIS);
  // Beim Ueben ist die KI frei: kein Sperrhinweis.
  assert.equal(sperrHinweis({ state: "active", mode: "training", task: { id: "t" } }), null);
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

// ---- Flaeche zuerst: Etiketten, Korrektur, Leiste ---------------------------------------------------
import { readFileSync } from "node:fs";
import {
  eingabeHinweis, ergebnisAus, ergebnisHinweis, etiketten, fehlerText, korrekturAntwort, KNOPF_TEXT,
  leistenZustand, speicherAusgang, speicherHinweis, speichernMitWiederholung,
} from "./aufgabe.js";

const SCHON = "Diese Aufgabe wurde schon abgegeben. Ich lade den aktuellen Stand.";
const FIXTURE = JSON.parse(readFileSync(new URL("../../tests/huelle/etiketten_fixture.json", import.meta.url), "utf8"));

test("leistenZustand: alle zehn Learning-Typen (gemeinsame Fixture mit Python)", () => {
  const { alle, deterministisch, seitenleiste } = FIXTURE.typen;
  assert.equal(alle.length, 10);
  for (const type of alle) {
    const z = leistenZustand({ type }, null);
    if (seitenleiste.includes(type)) { assert.equal(z.sichtbar, false, type); continue; }
    assert.equal(z.sichtbar, true, type);
    assert.ok(z.knoepfe.includes("abgeben"), type);
    const mitErgebnis = leistenZustand({ type }, ERG("Richtig."));
    assert.equal(mitErgebnis.knoepfe.includes("falsch"), deterministisch.includes(type), type);
  }
});

test("etiketten: gleiche Etiketten wie contracts/beschriftung.py (gemeinsame Fixture)", () => {
  for (const f of FIXTURE.etiketten) assert.deepEqual(etiketten(f.task), f.erwartet, f.name);
});

test("korrekturAntwort: gleiche Antworten wie antwort_aus_etiketten (gemeinsame Fixture)", () => {
  for (const f of FIXTURE.antworten) assert.deepEqual(korrekturAntwort(f.task, f.text), f.antwort, f.name);
});

test("korrekturAntwort: Brief-Faelle", () => {
  const drei = { type: "single_choice", options: [{ id: "x" }, { id: "y" }, { id: "z" }] };
  assert.deepEqual(korrekturAntwort({ type: "numeric" }, "−1,5"), { value: -1.5 });
  assert.equal(korrekturAntwort({ type: "numeric" }, ""), null);
  assert.deepEqual(korrekturAntwort(drei, "b"), { choice_id: "y" });
  assert.equal(korrekturAntwort(drei, "E"), null);
  assert.equal(korrekturAntwort(null, "A"), null);
});

test("korrekturAntwort: Trenner, Pfeile und doppelte linke Seite", () => {
  const m = { type: "matching", presentation: { left: [{ id: "l1" }, { id: "l2" }], right: [{ id: "r1" }, { id: "r2" }] } };
  assert.deepEqual(korrekturAntwort(m, "1=B; 2: a"), { pairs: { l1: "r2", l2: "r1" } });
  assert.equal(korrekturAntwort(m, "1-A, 1-B"), null);
  assert.equal(korrekturAntwort(m, "1 A"), null);
  const o = { type: "ordering", options: [{ id: "i1" }, { id: "i2" }] };
  assert.deepEqual(korrekturAntwort(o, "b a"), { order: ["i2", "i1"] });
});

test("korrekturAntwort: mehr als 26 Eintraege -> null (wie der ValueError der Bruecke)", () => {
  const options = Array.from({ length: 27 }, (_, i) => ({ id: `o${i}` }));
  assert.equal(korrekturAntwort({ type: "single_choice", options }, "A"), null);
  assert.throws(() => etiketten({ type: "single_choice", options }));
});

test("eingabeHinweis: je Typ ein Beispiel", () => {
  assert.equal(eingabeHinweis("multiple_choice"), "Buchstaben eingeben, z. B. A, C");
  assert.equal(eingabeHinweis("single_choice"), "Einen Buchstaben eingeben, z. B. B");
  assert.equal(eingabeHinweis("ordering"), "Reihenfolge eingeben, z. B. C, A, B");
  assert.equal(eingabeHinweis("matching"), "Paare eingeben, z. B. 1-B, 2-A");
  assert.equal(eingabeHinweis("numeric"), "Zahl eingeben, z. B. -1,5");
  assert.equal(eingabeHinweis("fill_in"), "Text eingeben");
  assert.equal(eingabeHinweis("code"), null);
});

const ERG = (urteil, lesbar = urteil !== "Nicht lesbar.") => ({ vorbewertung_id: "v1", urteil, lesbar });

test("leistenZustand: ohne Ergebnis Speichern und Abgeben, bei code keine Leiste", () => {
  assert.deepEqual(leistenZustand({ type: "numeric" }, null), { sichtbar: true, knoepfe: ["speichern", "abgeben"] });
  assert.deepEqual(leistenZustand({ type: "open" }, null), { sichtbar: true, knoepfe: ["speichern", "abgeben"] });
  assert.deepEqual(leistenZustand({ type: "code" }, null), { sichtbar: false, knoepfe: [] });
  assert.deepEqual(leistenZustand(null, null), { sichtbar: false, knoepfe: [] });
  assert.deepEqual(leistenZustand({ type: "unbekannt" }, null), { sichtbar: false, knoepfe: [] });
});

test("leistenZustand: lesbares Urteil -> Passt, Ueberarbeiten, Falsch gelesen (nur deterministisch)", () => {
  for (const urteil of ["Richtig.", "Teilweise richtig.", "Noch nicht richtig.", "Wird noch geprüft."]) {
    assert.deepEqual(leistenZustand({ type: "numeric" }, ERG(urteil)),
      { sichtbar: true, knoepfe: ["passt", "ueberarbeiten", "falsch", "speichern", "abgeben"] }, urteil);
    assert.deepEqual(leistenZustand({ type: "case" }, ERG(urteil)),
      { sichtbar: true, knoepfe: ["passt", "ueberarbeiten", "speichern", "abgeben"] }, urteil);
  }
});

test("leistenZustand: nicht lesbar -> Selbst eintippen statt Passt", () => {
  assert.deepEqual(leistenZustand({ type: "matching" }, ERG("Nicht lesbar.")),
    { sichtbar: true, knoepfe: ["ueberarbeiten", "eintippen", "speichern", "abgeben"] });
  // das lesbar-Flag allein reicht ebenfalls
  assert.deepEqual(leistenZustand({ type: "numeric" }, { vorbewertung_id: "v", urteil: "Richtig.", lesbar: false }).knoepfe,
    ["ueberarbeiten", "eintippen", "speichern", "abgeben"]);
  // Rubrik-Typ: nichts zum Eintippen
  assert.deepEqual(leistenZustand({ type: "open" }, ERG("Nicht lesbar.")).knoepfe,
    ["ueberarbeiten", "speichern", "abgeben"]);
});

test("KNOPF_TEXT: feste Beschriftungen", () => {
  assert.deepEqual(KNOPF_TEXT, {
    speichern: "Speichern", abgeben: "Abgeben", passt: "Passt", ueberarbeiten: "Überarbeiten",
    falsch: "Falsch gelesen", eintippen: "Selbst eintippen", neuBewerten: "Neu bewerten",
  });
});

test("ergebnisAus: Antwort der Bruecke -> Ergebnis, sonst null", () => {
  assert.deepEqual(ergebnisAus({ urteil: "Richtig.", vorbewertung: { id: "v9", lesbar: true } }),
    { vorbewertung_id: "v9", urteil: "Richtig.", lesbar: true });
  assert.equal(ergebnisAus({ urteil: "Nicht lesbar.", vorbewertung: { id: "v9", lesbar: false } }).lesbar, false);
  assert.equal(ergebnisAus({ urteil: "Richtig." }), null);
  assert.equal(ergebnisAus({ vorbewertung: { id: "v" } }), null);
  assert.equal(ergebnisAus(null), null);
});

test("ergebnisHinweis: nur bei nicht lesbar, mit Neuschreiben, Selbst eintippen und Wiederholungs-Warnung", () => {
  const t = ergebnisHinweis(ERG("Nicht lesbar."));
  assert.match(t, /Abgeben/);
  assert.match(t, /Selbst eintippen/);
  assert.match(t, /unverändert/i);
  assert.equal(ergebnisHinweis(ERG("Richtig.")), null);
  assert.equal(ergebnisHinweis(null), null);
});

test("speichernMitWiederholung: ok ohne Wiederholung", async () => {
  let n = 0;
  const r = await speichernMitWiederholung(async () => { n += 1; return { ok: true, grund: null, code: "gespeichert" }; });
  assert.deepEqual(r, { ok: true, art: "ok" });
  assert.equal(n, 1);
});

test("speichernMitWiederholung: nur der code geaendert wiederholt, genau einmal", async () => {
  const antworten = [{ ok: false, grund: "egal", code: "geaendert" }, { ok: true, grund: null, code: "gespeichert" }];
  let n = 0;
  const r = await speichernMitWiederholung(async () => antworten[n++]);
  assert.deepEqual(r, { ok: true, art: "ok" });
  assert.equal(n, 2);
  let m = 0;
  const r2 = await speichernMitWiederholung(async () => { m += 1; return { ok: false, code: "geaendert" }; });
  assert.deepEqual(r2, { ok: false, art: "fehler" });
  assert.equal(m, 2);
  // Klartext allein löst nichts mehr aus.
  let k = 0;
  const r3 = await speichernMitWiederholung(async () => { k += 1; return { ok: false, grund: "edited ungespeicherte Änderungen" }; });
  assert.deepEqual(r3, { ok: false, art: "fehler" });
  assert.equal(k, 1);
});

test("speichernMitWiederholung: leer und andere Codes ohne Wiederholung, wirft nie", async () => {
  let n = 0;
  const leer = await speichernMitWiederholung(async () => { n += 1; return { ok: false, grund: "x", code: "leer" }; });
  assert.deepEqual(leer, { ok: false, art: "leer" });
  for (const code of ["kein_knopf", "zeitueberschreitung", "fehler", "keine_flaeche", "keine_antwort"]) {
    const r = await speichernMitWiederholung(async () => { n += 1; return { ok: false, grund: "x", code }; });
    assert.deepEqual(r, { ok: false, art: "fehler" }, code);
  }
  assert.equal(n, 6);
  const wirft = await speichernMitWiederholung(async () => { throw new Error("x"); });
  assert.deepEqual(wirft, { ok: false, art: "fehler" });
});

test("speicherHinweis: ruhige Hinweise, nichts bei Erfolg", () => {
  assert.equal(speicherHinweis({ ok: true, art: "ok" }, true), null);
  assert.equal(speicherHinweis({ ok: false, art: "leer" }, true), "Auf der Fläche gibt es noch nichts zu speichern.");
  assert.equal(speicherHinweis({ ok: false, art: "fehler" }, true), "Nicht gespeichert – bitte „Speichern“ drücken.");
  assert.equal(speicherHinweis({ ok: false, art: "fehler" }, false), "Nicht gespeichert – bitte „Speichern“ drücken.");
  assert.equal(speicherHinweis({ ok: false, art: "leer" }, false), null);
});

test("fehlerText: 409 mit Turn aber anderer Ablehnung zeigt die Meldung der Bruecke", () => {
  const nicht = "Diese Abgabe wurde nicht angenommen. Bitte prüfen Sie Ihre Eingabe.";
  assert.deepEqual(fehlerText({ status: 409, daten: { turn: { state: "active" }, meldung: nicht } }), { text: nicht, veraltet: false });
});

test("fehlerText: 409 veraltet, 503 wiederholen, sonst feste Meldung oder allgemein", () => {
  const veraltet = fehlerText({ status: 409, daten: { turn: { state: "active" }, meldung: SCHON } });
  assert.equal(veraltet.veraltet, true);
  assert.match(veraltet.text, /veraltet/);
  const bw = "Die Bewertung ist gerade nicht erreichbar. Bitte geben Sie gleich noch einmal ab.";
  assert.deepEqual(fehlerText({ status: 503, daten: { meldung: bw } }), { text: bw, veraltet: false });
  assert.deepEqual(fehlerText({ status: 503, daten: { meldung: "Die Fläche ist gerade nicht erreichbar." } }),
    { text: "Die Fläche ist gerade nicht erreichbar.", veraltet: false });
  assert.equal(fehlerText({ status: 0, daten: {} }).text, ALLGEMEIN);
  assert.equal(fehlerText({ status: 409, daten: {} }).veraltet, false);
});

test("FESTE_MELDUNGEN kennt die neuen Meldungen der Bruecke", () => {
  for (const m of [
    "Diese Aufgabe beantworten Sie in der Seitenleiste.",
    "Bei dieser Aufgabe gibt es nichts Gelesenes zu korrigieren.",
    "Die Bewertung ist gerade nicht erreichbar. Bitte geben Sie gleich noch einmal ab.",
    "In der Hülle können Sie nur üben. Diese Sitzung lässt keine Vorbewertung zu.",
    "Diese Rückmeldung lässt sich nicht bestätigen. Bitte überarbeiten Sie Ihr Ergebnis oder tippen Sie es selbst ein.",
  ]) assert.ok(FESTE_MELDUNGEN.has(m), m);
});

test("speicherAusgang: Fehler vor 'Weiter' setzt den Dauerhinweis, der danach stehen bleibt", () => {
  const fehler = { ok: false, art: "fehler" };
  const nachPasst = speicherAusgang(fehler, { manuell: false, token: 1, aktuell: 1, alt: null });
  assert.deepEqual(nachPasst, { speicher: "Nicht gespeichert – bitte „Speichern“ drücken.", meldung: null });
  // Ein späteres automatisches „leer“ oder ein fremdes Ergebnis lässt ihn unverändert.
  assert.equal(speicherAusgang({ ok: false, art: "leer" }, { manuell: false, token: 1, aktuell: 1, alt: nachPasst.speicher }).speicher, nachPasst.speicher);
});

test("speicherAusgang: Fehler nach der Wartezeit von Passt landet ebenfalls im Dauerhinweis (Token noch aktuell)", () => {
  assert.equal(speicherAusgang({ ok: false, art: "fehler" }, { manuell: false, token: 3, aktuell: 3 }).speicher, "Nicht gespeichert – bitte „Speichern“ drücken.");
});

test("speicherAusgang: Ergebnis eines anderen Kurses wird verworfen", () => {
  assert.deepEqual(speicherAusgang({ ok: false, art: "fehler" }, { manuell: true, token: 1, aktuell: 2, alt: null }), { speicher: null, meldung: null });
  assert.deepEqual(speicherAusgang({ ok: true, art: "ok" }, { manuell: true, token: 1, aktuell: 2, alt: "alt" }), { speicher: "alt", meldung: null });
});

test("speicherAusgang: Erfolg löscht den Dauerhinweis; Meldung nur beim Knopf; leer nur beim Knopf", () => {
  assert.deepEqual(speicherAusgang({ ok: true, art: "ok" }, { manuell: false, token: 1, aktuell: 1, alt: "x" }), { speicher: null, meldung: null });
  assert.deepEqual(speicherAusgang({ ok: true, art: "ok" }, { manuell: true, token: 1, aktuell: 1, alt: "x" }), { speicher: null, meldung: "Gespeichert." });
  assert.deepEqual(speicherAusgang({ ok: false, art: "leer" }, { manuell: true, token: 1, aktuell: 1 }),
    { speicher: null, meldung: "Auf der Fläche gibt es noch nichts zu speichern." });
  assert.deepEqual(speicherAusgang({ ok: false, art: "leer" }, { manuell: false, token: 1, aktuell: 1 }), { speicher: null, meldung: null });
});

import { ohneLeere } from "./aufgabe.js";

test("ohneLeere: null und undefined fallen weg, sonst bleibt die Reihenfolge", () => {
  // Live 06.11.: ohne Sperrhinweis stand "null" als Text in der Aufgabenkarte.
  const a = { n: 1 };
  const b = { n: 2 };
  assert.deepEqual(ohneLeere(a, null, b, undefined), [a, b]);
  assert.deepEqual(ohneLeere(null), []);
});

test("huelle.js gibt die Aufgabenkarte nur ohne leere Teile weiter", () => {
  const quelle = readFileSync(new URL("./huelle.js", import.meta.url), "utf8");
  assert.match(quelle, /\$\("karte"\)\.replaceChildren\(\.\.\.ohneLeere\(/);
});

test("Karte nicht auf der Fläche: Urteil als Hinweis in der Leiste", () => {
  const e = ergebnisAus({ urteil: "Richtig.", vorbewertung: { id: "v9", lesbar: true }, flaeche: false, karte: "Gelesen: D Richtig." });
  assert.equal(e.karte, "Gelesen: D Richtig.");
  assert.equal(ergebnisHinweis(e), "Gelesen: D Richtig.");
  assert.equal("karte" in ergebnisAus({ urteil: "Richtig.", vorbewertung: { id: "v9" }, flaeche: true }), false);
  const unl = ergebnisAus({ urteil: "Nicht lesbar.", vorbewertung: { id: "v9", lesbar: false }, karte: "Nicht lesbar." });
  assert.match(ergebnisHinweis(unl), /Selbst eintippen/);
});
