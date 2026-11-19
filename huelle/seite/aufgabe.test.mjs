import test from "node:test";
import assert from "node:assert/strict";
import { abgabeNummerZulaessig, autoVerbindenFaellig, antwortAus, fokusZiel, formel, klartext, parseZahl } from "./aufgabe.js";

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

// Dieselbe Fall-Liste prueft auch die Python-Seite (tests/huelle/test_zusammenfassung.py): PDF und Seitenleiste
// zeigen jeden Begriff gleich. Live 09.11.: „Hebbare definitionsluecke“ statt „Hebbare Definitionslücke“.
test("begriffTitel: Slug wird lesbarer deutscher Titel, alles andere bleibt (gemeinsame Fall-Liste)", async () => {
  const { readFile } = await import("node:fs/promises");
  const faelle = JSON.parse(await readFile(new URL("../../tests/huelle/begriff_titel_faelle.json", import.meta.url), "utf8"));
  assert.ok(faelle.length >= 30);
  for (const [roh, erwartet] of faelle) assert.equal(begriffTitel(roh), erwartet, roh);
  assert.equal(begriffTitel(null), "");
});

import { begriffeZusammenfassen, flaechenHinweis } from "./aufgabe.js";

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

test("flaechenHinweis: auch am Kursende, wenn das Kurs-Canvas nicht nach vorn kam (Abnahme F3)", () => {
  const ende = { state: "completed" };
  assert.equal(flaechenHinweis({ flaeche: false, abgeschlossen: true, turn: ende }), true);
  assert.equal(flaechenHinweis({ flaeche: true, abgeschlossen: true, turn: ende }), false);
  assert.equal(flaechenHinweis({ abgeschlossen: true, turn: ende }), false);
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

test("meldungFuer: Fehlercodes je Aufgaben-Canvas haben feste Texte", () => {
  assert.equal(meldungFuer({ fehler: "FLAECHE_BESCHAEFTIGT" }), "Bitte kurz warten und erneut abgeben.");
  assert.equal(meldungFuer({ fehler: "KEINE_FLAECHE" }), "Bitte die Lernfläche in diesem Browser öffnen.");
  assert.equal(meldungFuer({ fehler: "KURS_ABGEBEN_ALT" }), "Für diesen Kurs gibt es „Kurs abgeben“ noch nicht.");
  for (const m of [
    "Bitte kurz warten und erneut abgeben.",
    "Bitte die Lernfläche in diesem Browser öffnen.",
    "Für diesen Kurs gibt es „Kurs abgeben“ noch nicht.",
  ]) assert.ok(FESTE_MELDUNGEN.has(m), m);
  // Unbekannte Codes und Eigenschaften des Objekt-Prototyps bleiben allgemein.
  assert.equal(meldungFuer({ fehler: "UNBEKANNT" }), ALLGEMEIN);
  assert.equal(meldungFuer({ fehler: "constructor" }), ALLGEMEIN);
  assert.equal(meldungFuer({ fehler: "kein_zugang", meldung: "Ungültige Eingabe." }), "Ungültige Eingabe.");
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

import { leistenKnoepfe } from "./aufgabe.js";

test("leistenKnoepfe: gleiche Lage, gleiche Kennung; nur echte Änderungen bauen die Leiste neu (A2, Befund 8)", () => {
  const lage = { busy: false, speichert: false, abgebenGesperrt: false };
  const a = leistenKnoepfe(["passt", "eintippen", "abgeben"], lage);
  const b = leistenKnoepfe(["passt", "eintippen", "abgeben"], { ...lage });
  assert.equal(a.kennung, b.kennung);
  assert.deepEqual(a.knoepfe.map((k) => k.id), ["passt", "eintippen", "abgeben"]);
  assert.deepEqual(a.knoepfe[1], { id: "eintippen", haupt: false, disabled: false, text: "Selbst eintippen" });
  assert.equal(a.knoepfe[2].haupt, true);
  assert.notEqual(leistenKnoepfe(["passt", "eintippen", "abgeben"], { ...lage, abgebenGesperrt: true }).kennung, a.kennung);
  assert.notEqual(leistenKnoepfe(["passt", "abgeben"], lage).kennung, a.kennung);
  const beschaeftigt = leistenKnoepfe(["speichern", "abgeben"], { ...lage, busy: true });
  assert.ok(beschaeftigt.knoepfe.every((k) => k.disabled));
  assert.equal(leistenKnoepfe(["speichern"], { ...lage, speichert: true }).knoepfe[0].disabled, true);
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

import { kursVorwahl } from "./aufgabe.js";

test("meldungFuer: Texte der Kursuebersicht (Teil B) bleiben", () => {
  for (const m of [
    "Ihr Zugang zu diesem Kurs besteht nicht mehr. Öffnen Sie die Kursübersicht.",
    "Die Kursübersicht ist gerade nicht erreichbar. Bitte versuchen Sie es gleich noch einmal.",
  ]) {
    assert.ok(FESTE_MELDUNGEN.has(m), m);
    assert.equal(meldungFuer({ fehler: "x", text: m, meldung: m }), m);
  }
});

test("kursVorwahl: ?kurs= vor gemerktem Kurs, nur Kurse der Liste", () => {
  const kurse = [{ course_id: "a-1" }, { course_id: "b-2" }];
  assert.equal(kursVorwahl("?kurs=b-2", kurse, "a-1"), "b-2");
  assert.equal(kursVorwahl("?kurs=fremd", kurse, "a-1"), "a-1");
  assert.equal(kursVorwahl("?x=1", kurse, "b-2"), "b-2");
  assert.equal(kursVorwahl("", kurse, "weg"), "a-1");
  assert.equal(kursVorwahl("?kurs=", kurse, null), "a-1");
  assert.equal(kursVorwahl("?kurs=b-2", [], null), null);
});


test("FESTE_MELDUNGEN kennt die Texte der Gruppenfläche (Teil C)", () => {
  for (const m of [
    "Sie gehören nicht zu dieser Gruppe. Öffnen Sie die Kursübersicht.",
    "Die Gruppenfläche ist gerade nicht erreichbar. Versuchen Sie es in einer Minute noch einmal.",
    "Diese Gruppenfläche ist voll. Bitte wenden Sie sich an Ihre Kursleitung.",
  ]) {
    assert.ok(FESTE_MELDUNGEN.has(m), m);
    assert.equal(meldungFuer({ meldung: m }), m);
  }
});
test("abgabeNummerZulaessig: anpassender Kurs nur die aktuelle Aufgabe", () => {
  const z = { liste: false, aktuell: 3 };
  assert.equal(abgabeNummerZulaessig(3, z), true);
  assert.equal(abgabeNummerZulaessig(2, z), false);
  assert.equal(abgabeNummerZulaessig(4, z), false);
  assert.equal(abgabeNummerZulaessig(3, { liste: false, aktuell: null }), false);
});

test("abgabeNummerZulaessig: Listenkurs nur noch nicht gebuchte Aufgaben", () => {
  const z = { liste: true, aufgaben: [
    { nummer: 1, status: "bestaetigt" }, { nummer: 2, status: "offen" },
    { nummer: 3, status: "rueckmeldung" }, { nummer: 4, status: "in_bewertung" }, { nummer: 5, status: "fehler" },
  ] };
  assert.equal(abgabeNummerZulaessig(1, z), false);
  assert.equal(abgabeNummerZulaessig(2, z), true);
  assert.equal(abgabeNummerZulaessig(3, z), true); // Ueberarbeiten: noch nicht mit „Passt“ gebucht
  assert.equal(abgabeNummerZulaessig(4, z), false);
  assert.equal(abgabeNummerZulaessig(5, z), true);
  assert.equal(abgabeNummerZulaessig(9, z), false);
  assert.equal(abgabeNummerZulaessig(0, z), false);
  assert.equal(abgabeNummerZulaessig(2.5, z), false);
  assert.equal(abgabeNummerZulaessig(2, null), false);
  assert.equal(abgabeNummerZulaessig(2, { liste: true }), false);
});

test("autoVerbindenFaellig: beide Reihenfolgen, einmal je Laden", () => {
  // Nachricht vor dem Hinweis: noch nicht faellig, dann faellig sobald der Hinweis steht
  assert.equal(autoVerbindenFaellig({ mcpBereit: true, hinweisSichtbar: false, schonAutoVerbunden: false }), false);
  assert.equal(autoVerbindenFaellig({ mcpBereit: true, hinweisSichtbar: true, schonAutoVerbunden: false }), true);
  // Hinweis vor der Nachricht
  assert.equal(autoVerbindenFaellig({ mcpBereit: false, hinweisSichtbar: true, schonAutoVerbunden: false }), false);
  // nie ein zweites Mal
  assert.equal(autoVerbindenFaellig({ mcpBereit: true, hinweisSichtbar: true, schonAutoVerbunden: true }), false);
  assert.equal(autoVerbindenFaellig(), false);
});

// ---- Begriffs-Kärtchen (Seitenleiste) ----------------------------------------------------------------
import {
  KAERTCHEN_TYPEN, kaertchenAntwort, kaertchenKennung, kaertchenPaar, kaertchenStart, kaertchenText, kaertchenTippen,
} from "./aufgabe.js";

const OPT3 = [{ id: "o1", label: "x" }, { id: "o2", label: "y" }, { id: "o3", label: "z" }];
const KT = (type) => ({ id: "t1", type, presentation: {}, options: OPT3 });
const KMATCH = {
  id: "t2", type: "matching", options: [],
  presentation: { left: [{ id: "l1", label: "a" }, { id: "l2", label: "b" }], right: [{ id: "r1", label: "c" }, { id: "r2", label: "d" }] },
};
const tippe = (task, folge) => folge.reduce((zu, e) => kaertchenTippen(task, zu, e), kaertchenStart(task));

test("KAERTCHEN_TYPEN: die vier Auswahl-Typen", () => {
  assert.deepEqual([...KAERTCHEN_TYPEN].sort(), ["matching", "multiple_choice", "ordering", "single_choice"]);
});

test("kaertchenAntwort single: Tippen ersetzt", () => {
  const t = KT("single_choice");
  assert.deepEqual(kaertchenAntwort(t, tippe(t, ["B"])), { choice_id: "o2" });
  assert.deepEqual(kaertchenAntwort(t, tippe(t, ["B", "C"])), { choice_id: "o3" });
  assert.equal(kaertchenText(t, tippe(t, ["B", "C"])), "C");
});

test("kaertchenAntwort multiple: Tippen schaltet um", () => {
  const t = KT("multiple_choice");
  assert.deepEqual(kaertchenAntwort(t, tippe(t, ["A", "C", "A"])), { choice_ids: ["o3"] });
  assert.equal(kaertchenText(t, tippe(t, ["C", "A"])), "A, C");
});

test("kaertchenAntwort ordering: Tipp-Reihenfolge, erneutes Tippen nimmt heraus", () => {
  const t = KT("ordering");
  assert.deepEqual(kaertchenAntwort(t, tippe(t, ["C", "A", "B"])), { order: ["o3", "o1", "o2"] });
  assert.equal(kaertchenText(t, tippe(t, ["C", "A", "B"])), "C, A, B");
  assert.equal(kaertchenText(t, tippe(t, ["C", "A", "C"])), "A");
  const teil = tippe(t, ["C", "A"]);
  assert.deepEqual(kaertchenAntwort(t, teil), korrekturAntwort(t, "C, A"));
});

test("kaertchenAntwort matching: Paare, zweites Tippen löscht", () => {
  const z = kaertchenPaar(kaertchenPaar(kaertchenStart(KMATCH), "2", "A"), "1", "B");
  assert.equal(kaertchenText(KMATCH, z), "1-B, 2-A");
  assert.deepEqual(kaertchenAntwort(KMATCH, z), { pairs: { l1: "r2", l2: "r1" } });
  assert.equal(kaertchenAntwort(KMATCH, kaertchenPaar(kaertchenStart(KMATCH), "1", "B")).pairs.l1, "r2");
  const weg = kaertchenPaar(kaertchenPaar(kaertchenStart(KMATCH), "1", "B"), "1", "B");
  assert.equal(kaertchenAntwort(KMATCH, weg), null);
  const um = kaertchenPaar(kaertchenPaar(kaertchenStart(KMATCH), "1", "B"), "1", "A");
  assert.equal(kaertchenText(KMATCH, um), "1-A");
  assert.equal(kaertchenAntwort(KMATCH, kaertchenStart(KMATCH)), null);
});

test("kaertchenTippen und kaertchenPaar verändern den Zustand nicht", () => {
  const t = KT("multiple_choice");
  const z0 = kaertchenStart(t);
  kaertchenTippen(t, z0, "A");
  assert.deepEqual(z0, { wahl: [], paare: {} });
  const m0 = kaertchenStart(KMATCH);
  kaertchenPaar(m0, "1", "A");
  assert.deepEqual(m0, { wahl: [], paare: {} });
});

test("kaertchenKennung ändert sich nur bei Zustandswechsel", () => {
  const t = KT("multiple_choice");
  assert.equal(kaertchenKennung(t, tippe(t, ["A"])), kaertchenKennung(t, tippe(t, ["A"])));
  assert.notEqual(kaertchenKennung(t, tippe(t, ["A"])), kaertchenKennung(t, tippe(t, ["A", "B"])));
  assert.notEqual(kaertchenKennung(KMATCH, kaertchenStart(KMATCH)), kaertchenKennung(KMATCH, kaertchenPaar(kaertchenStart(KMATCH), "1", "A")));
});

test("kaertchenStart: ohne Kärtchen-Typ null", () => {
  assert.equal(kaertchenStart({ id: "n", type: "numeric", presentation: {} }), null);
  assert.equal(kaertchenAntwort({ id: "n", type: "numeric", presentation: {} }, null), null);
  assert.equal(kaertchenAntwort(null, null), null);
});

test("fokusZiel: findet den Knopf mit demselben Schlüssel nach dem Neubau", () => {
  const knoepfe = [{ dataset: { fokus: "A" } }, { dataset: { fokus: "B" } }, { dataset: { fokus: "1-B" } }];
  assert.equal(fokusZiel(knoepfe, "B"), knoepfe[1]);
  assert.equal(fokusZiel(knoepfe, "1-B"), knoepfe[2]);
});

test("fokusZiel: ohne Schlüssel oder ohne Treffer null", () => {
  const knoepfe = [{ dataset: { fokus: "A" } }, { dataset: {} }, {}];
  assert.equal(fokusZiel(knoepfe, "Z"), null);
  assert.equal(fokusZiel(knoepfe, null), null);
  assert.equal(fokusZiel(knoepfe, ""), null);
  assert.equal(fokusZiel([], "A"), null);
});

// Code-Ausfuehrung (15.11.): Programmieraufgaben laufen lokal im Code-Laeufer; die Seite zeigt nur das Ergebnis.
import { CODE_SPRACHEN, laufText } from "./aufgabe.js";

test("laufText: Ausgabe, Fehlerausgabe und Abschlusszeile", () => {
  assert.equal(laufText({ exit: 0, stdout: "42\n", stderr: "", zeit_ueberschritten: false, abgeschnitten: false, dauer_ms: 17 }),
    "42\n\n— Fertig (17 ms)");
  assert.equal(laufText({ exit: 1, stdout: "", stderr: "ZeroDivisionError\n", zeit_ueberschritten: false, abgeschnitten: false, dauer_ms: 20 }),
    "ZeroDivisionError\n\n— Beendet mit Fehler (Code 1, 20 ms)");
  assert.equal(laufText({ exit: 0, stdout: "", stderr: "", zeit_ueberschritten: false, abgeschnitten: false, dauer_ms: 5 }),
    "(keine Ausgabe)\n\n— Fertig (5 ms)");
});

test("laufText: Zeitueberschreitung und gekuerzte Ausgabe stehen in Worten da", () => {
  assert.match(laufText({ exit: null, stdout: "a\n", stderr: "", zeit_ueberschritten: true, abgeschnitten: false, dauer_ms: 5000 }),
    /^a\n\n— Abgebrochen: Das Programm lief länger als erlaubt\.$/);
  assert.match(laufText({ exit: 0, stdout: "x", stderr: "", zeit_ueberschritten: false, abgeschnitten: true, dauer_ms: 9 }),
    /Die Ausgabe wurde gekürzt\./);
});

test("laufText: unbrauchbare Antwort ergibt einen ruhigen Satz", () => {
  assert.equal(laufText(null), "Das Ergebnis ließ sich nicht lesen.");
  assert.equal(laufText({ stdout: 5 }), "Das Ergebnis ließ sich nicht lesen.");
});

test("CODE_SPRACHEN: genau die Sprachen des Laeufers", () => {
  assert.deepEqual(CODE_SPRACHEN.map((s) => s.wert), ["python", "javascript"]);
});

test("huelle.js: Programmieraufgabe hat Ausfuehren, der Code geht nur an die Route der Sitzung (Quelltext)", async () => {
  const { readFileSync } = await import("node:fs");
  const js = readFileSync(new URL("./huelle.js", import.meta.url), "utf8");
  const lauf = js.split("async function codeAusfuehren(")[1].split("\n}\n")[0];
  assert.match(lauf, /const auftrag = \{ sprache, quelltext, erlaubt \};/);
  assert.match(lauf, /api\("POST", `\/sitzungen\/\$\{[^}]+\}\/ausfuehren`, auftrag\)/);
  assert.match(lauf, /laufText\(r\.daten\)/);
  assert.match(js, /id: "ausfuehren"/);
});
test("kursErgebnis: zählt die Urteile der Engine, teilweise nur wenn vorhanden", async () => {
  const { kursErgebnis } = await import("./aufgabe.js");
  const s = (...states) => ({ results: states.map((state, i) => ({ ordinal: i + 1, score: 0.5, state })) });
  assert.equal(kursErgebnis(s("correct", "correct", "partial", "incorrect")), "2 von 4 Aufgaben richtig, 1 teilweise.");
  assert.equal(kursErgebnis(s("correct", "incorrect")), "1 von 2 Aufgaben richtig.");
  assert.equal(kursErgebnis(s("correct")), "1 von 1 Aufgabe richtig.");
});

test("kursErgebnis: ohne Urteil der Engine gelten die alten Grenzen; ohne Ergebnisse null", async () => {
  const { kursErgebnis } = await import("./aufgabe.js");
  assert.equal(kursErgebnis({ results: [{ score: 1 }, { score: 0.7 }, { score: 0.2 }] }), "1 von 3 Aufgaben richtig, 1 teilweise.");
  assert.equal(kursErgebnis({ results: [] }), null);
  assert.equal(kursErgebnis(null), null);
  assert.equal(kursErgebnis({ results: "x" }), null);
});

test("Kärtchen: die abgegebene Wahl bleibt je Sitzung und Aufgabe gemerkt (Listenkurs)", async () => {
  const { readFileSync } = await import("node:fs");
  const js = readFileSync(new URL("./huelle.js", import.meta.url), "utf8");
  assert.match(js, /z\.kaertchenAbgegeben\.set\(`\$\{t\.session_id\}\|\$\{task\.id\}`, z\.kaertchen\.zustand\)/);
  assert.match(js, /abgegeben \?\? kaertchenStart\(task\)/);
  // Im Listenkurs wird die Wahl nach dem Abgeben nicht mehr verworfen.
  const start = js.indexOf("async function kaertchenAbgeben()");
  const listenteil = js.slice(start, js.indexOf("void speichern(false);", start));
  assert.ok(listenteil.length > 0 && !listenteil.includes("z.kaertchen = null"));
});

test("codeVorgabe: Sprache und Startcode der Aufgabe, nur bekannte Sprachen", async () => {
  const { codeVorgabe } = await import("./aufgabe.js");
  assert.deepEqual(codeVorgabe({ type: "code", presentation: { sprache: "javascript", startcode: "// los\n" } }),
    { sprache: "javascript", startcode: "// los\n" });
  // Ältere Code-Aufgaben ohne Angabe: freie Sprachwahl, leeres Feld.
  assert.deepEqual(codeVorgabe({ type: "code", presentation: {} }), { sprache: null, startcode: "" });
  assert.deepEqual(codeVorgabe({ type: "code" }), { sprache: null, startcode: "" });
  assert.deepEqual(codeVorgabe({ type: "code", presentation: { sprache: "bash", startcode: 7 } }), { sprache: null, startcode: "" });
  assert.deepEqual(codeVorgabe(null), { sprache: null, startcode: "" });
});

test("Code-Karte: Sprache der Aufgabe ist fest, der Startcode steht im Feld", async () => {
  const { readFileSync } = await import("node:fs");
  const js = readFileSync(new URL("./huelle.js", import.meta.url), "utf8");
  assert.match(js, /const vorgabe = codeVorgabe\(task\);/);
  assert.match(js, /sprache\.value = vorgabe\.sprache;\s+sprache\.disabled = true;/);
  assert.match(js, /feld\.value = vorgabe\.startcode;/);
});

// ---- Erlaubnisse für Programmieraufgaben ------------------------------------------------------------
const CODE_TASK = (erlaubnisse) => ({ type: "code", presentation: { sprache: "python", erlaubnisse } });

test("codeObergrenze: nur bekannte Erlaubnisse der Aufgabe, in fester Reihenfolge", async () => {
  const { codeObergrenze } = await import("./aufgabe.js");
  assert.deepEqual(codeObergrenze(CODE_TASK(["daten", "pakete", "netz", 7])), ["pakete", "daten"]);
  assert.deepEqual(codeObergrenze(CODE_TASK(undefined)), []);
  assert.deepEqual(codeObergrenze({ type: "code" }), []);
  assert.deepEqual(codeObergrenze(null), []);
});

test("codeWuensche: erkennt Pakete, Diagramm und Übungsdaten, aber nur innerhalb der Obergrenze", async () => {
  const { codeWuensche } = await import("./aufgabe.js");
  const alle = ["pakete", "diagramm", "lange_laufzeit", "daten"];
  const code = "import numpy as np\nfrom matplotlib import pyplot as plt\nplt.savefig('ausgabe/k.png')\nopen('daten/u.csv')";
  assert.deepEqual(codeWuensche(code, "python", alle), ["pakete", "diagramm", "daten"]);
  assert.deepEqual(codeWuensche(code, "python", ["pakete"]), ["pakete"]);
  assert.deepEqual(codeWuensche(code, "python", []), []);
  assert.deepEqual(codeWuensche("print(1)", "python", alle), []);
  // Kein Import: das Wort allein (Kommentar, Zeichenkette) ist kein Wunsch nach Paketen.
  assert.deepEqual(codeWuensche("# numpy waere schoen\nprint('matplotlib')", "python", alle), []);
  assert.deepEqual(codeWuensche("  import matplotlib.pyplot as plt", "python", alle), ["pakete"]);
  // Pakete gibt es nur fuer Python; eine laengere Laufzeit laesst sich am Text nicht erkennen.
  assert.deepEqual(codeWuensche("import numpy", "javascript", alle), []);
  assert.deepEqual(codeWuensche("while True: pass", "python", alle), []);
});

test("erlaubnisStand: was schon für die Aufgabe erlaubt ist und wonach noch gefragt werden muss", async () => {
  const { erlaubnisStand } = await import("./aufgabe.js");
  assert.deepEqual(erlaubnisStand(["pakete", "diagramm"], ["pakete"]), { erlaubt: ["pakete"], offen: ["diagramm"] });
  assert.deepEqual(erlaubnisStand(["pakete"], ["pakete", "daten"]), { erlaubt: ["pakete"], offen: [] });
  assert.deepEqual(erlaubnisStand([], ["pakete"]), { erlaubt: [], offen: [] });
  assert.deepEqual(erlaubnisStand(["daten"], undefined), { erlaubt: [], offen: ["daten"] });
});

test("erlaubnisFrage: ein Satz, der sagt, was der Code möchte", async () => {
  const { erlaubnisFrage } = await import("./aufgabe.js");
  assert.equal(erlaubnisFrage(["pakete"]), "Dieser Code möchte Pakete laden (numpy, matplotlib).");
  assert.equal(erlaubnisFrage(["pakete", "diagramm"]),
    "Dieser Code möchte Pakete laden (numpy, matplotlib) und ein Diagramm ausgeben.");
  assert.equal(erlaubnisFrage(["pakete", "diagramm", "daten"]),
    "Dieser Code möchte Pakete laden (numpy, matplotlib), ein Diagramm ausgeben und Übungsdaten lesen.");
  assert.equal(erlaubnisFrage(["lange_laufzeit"]), "Dieser Code möchte länger laufen (bis 20 Sekunden).");
});

test("laengerAnbieten: nach abgelaufener Zeit, wenn die Aufgabe es zulässt und es noch nicht galt", async () => {
  const { laengerAnbieten } = await import("./aufgabe.js");
  const ab = { zeit_ueberschritten: true, erlaubt: [] };
  assert.equal(laengerAnbieten(ab, ["lange_laufzeit"]), true);
  assert.equal(laengerAnbieten(ab, ["pakete"]), false);
  assert.equal(laengerAnbieten({ zeit_ueberschritten: true, erlaubt: ["lange_laufzeit"] }, ["lange_laufzeit"]), false);
  assert.equal(laengerAnbieten({ zeit_ueberschritten: false, erlaubt: [] }, ["lange_laufzeit"]), false);
  assert.equal(laengerAnbieten(null, ["lange_laufzeit"]), false);
});

test("laufBilder: nur brauchbare PNG-Einträge bleiben", async () => {
  const { laufBilder } = await import("./aufgabe.js");
  const gut = { name: "kurve.png", png_base64: "iVBORw0KGgo=" };
  assert.deepEqual(laufBilder({ bilder: [gut, { name: "x.png" }, { name: 5, png_base64: "a" }, null, { name: "b.png", png_base64: "<script>" }] }),
    [{ name: "kurve.png", base64: "iVBORw0KGgo=" }]);
  assert.deepEqual(laufBilder({}), []);
  assert.deepEqual(laufBilder(null), []);
});

test("laufText: fehlendes Paket ohne Erlaubnis bekommt einen Hinweis", async () => {
  const { laufText } = await import("./aufgabe.js");
  const daten = { exit: 1, stdout: "", stderr: "ModuleNotFoundError: No module named 'numpy'", dauer_ms: 20, erlaubt: [] };
  assert.match(laufText(daten), /Für numpy und matplotlib braucht dieser Code die Erlaubnis „Pakete“\./);
  assert.doesNotMatch(laufText({ ...daten, erlaubt: ["pakete"] }), /Erlaubnis/);
  assert.doesNotMatch(laufText({ ...daten, stderr: "ModuleNotFoundError: No module named 'pandas'" }), /Erlaubnis/);
});

test("Code-Karte: Rückfrage vor dem Lauf und vor der Abgabe, Bilder als Objekt-Adressen", async () => {
  const { readFileSync } = await import("node:fs");
  const js = readFileSync(new URL("./huelle.js", import.meta.url), "utf8");
  // Ausführen und Abgeben klären die Erlaubnis über dieselbe Funktion.
  assert.match(js, /const erlaubt = await codeErlaubnis\(task, sprache, quelltext, frage, zusaetzlich\);/);
  assert.match(js, /codeErlaubnis\(task, sprache\.value, feld\.value, frage\)\.then\(\(erlaubt\) => antwortSenden\(antwort, erlaubt\)\)/);
  for (const knopf of ["erlaubnis-einmal", "erlaubnis-aufgabe", "erlaubnis-nein", "laenger-laufen"]) {
    assert.ok(js.includes(`id: "${knopf}"`), knopf);
  }
  // „Ablehnen“ gibt nur weiter, was für die Aufgabe schon erlaubt war.
  assert.match(js, /fertig\(\[\.\.\.\(antwort === "nein" \? erlaubt : wuensche\), \.\.\.fest\]\);/);
  // Nur „Für diese Aufgabe erlauben“ merkt sich die Zustimmung.
  assert.match(js, /if \(antwort === "aufgabe"\) \{\s+z\.codeErlaubt\.set\(/);
  // Bilder: keine data:-Adressen (die Seite erlaubt nur 'self' und blob:).
  assert.ok(js.includes("URL.createObjectURL(new Blob([roh], { type: \"image/png\" }))"));
  assert.ok(!js.includes("data:image"));
});

test("codeOhneFrage: was der Kurs ohne Rückfrage erlaubt, nur innerhalb der Obergrenze der Aufgabe", async () => {
  const { codeOhneFrage } = await import("./aufgabe.js");
  const task = (erlaubnisse, ohne_frage) => ({ type: "code", presentation: { erlaubnisse, ohne_frage } });
  assert.deepEqual(codeOhneFrage(task(["pakete", "diagramm"], ["diagramm", "pakete"])), ["pakete", "diagramm"]);
  // Was die Aufgabe nicht vorsieht, gilt nie, auch wenn es in der Liste steht.
  assert.deepEqual(codeOhneFrage(task(["pakete"], ["pakete", "daten", "netz"])), ["pakete"]);
  assert.deepEqual(codeOhneFrage(task(["pakete"], undefined)), []);
  assert.deepEqual(codeOhneFrage(task(["pakete"], "pakete")), []);
  assert.deepEqual(codeOhneFrage(null), []);
});

test("ohneFrageHinweis: ein Satz, der sagt, was in diesem Kurs ohne Rückfrage gilt", async () => {
  const { ohneFrageHinweis } = await import("./aufgabe.js");
  assert.equal(ohneFrageHinweis(["pakete", "diagramm"]),
    "In diesem Kurs darf Ihr Code ohne Rückfrage: Pakete laden (numpy, matplotlib) und ein Diagramm ausgeben.");
  assert.equal(ohneFrageHinweis(["daten"]), "In diesem Kurs darf Ihr Code ohne Rückfrage: Übungsdaten lesen.");
  assert.equal(ohneFrageHinweis([]), "");
});

test("Code-Karte: was der Kurs ohne Rückfrage erlaubt, gilt bei jedem Lauf und steht als Hinweis da", async () => {
  const { readFileSync } = await import("node:fs");
  const js = readFileSync(new URL("./huelle.js", import.meta.url), "utf8");
  // Ohne Frage Erlaubtes zählt wie schon zugestimmt; nach dem Rest wird weiter gefragt.
  assert.match(js, /const fest = \[\.\.\.new Set\(\[\.\.\.zusaetzlich, \.\.\.codeOhneFrage\(task\)\]\)\]\.filter\(\(e\) => obergrenze\.includes\(e\)\);/);
  assert.match(js, /ohneFrageHinweis\(codeOhneFrage\(task\)\)/);
});
test("leistenText: kurzer Text bleibt ganz, langer wird an einer Wortgrenze gekürzt und trägt den Rest", async () => {
  const { leistenText, LEISTE_ZEICHEN } = await import("./aufgabe.js");
  assert.deepEqual(leistenText("Richtig.\n\nGut."), { kurz: "Richtig. Gut.", voll: null });
  assert.deepEqual(leistenText(null), { kurz: "", voll: null });
  assert.deepEqual(leistenText("   "), { kurz: "", voll: null });
  const lang = "Teilweise richtig.\n\n" + Array.from({ length: 30 }, (_, i) => `Satz ${i}: Die Begründung fehlt noch.`).join(" ");
  const { kurz, voll } = leistenText(lang);
  assert.ok(kurz.length <= LEISTE_ZEICHEN && kurz.startsWith("Teilweise richtig. Satz 0:") && kurz.endsWith(" …"));
  // Kein halbes Wort am Ende, und der ganze Text bleibt mit seinen Absätzen erhalten.
  assert.ok(lang.replace(/\s+/g, " ").startsWith(kurz.slice(0, -2)));
  assert.match(kurz, /[A-Za-zäöüß.:]\s…$/);
  assert.equal(voll, lang);
  // Genau an der Grenze: nichts zu kürzen.
  const grenze = "a".repeat(LEISTE_ZEICHEN);
  assert.deepEqual(leistenText(grenze), { kurz: grenze, voll: null });
  // Ein Wort ohne Leerzeichen wird hart gekürzt statt gar nicht.
  const wurm = leistenText("b".repeat(LEISTE_ZEICHEN + 50));
  assert.ok(wurm.kurz.length <= LEISTE_ZEICHEN && wurm.kurz.endsWith(" …") && wurm.voll.length === LEISTE_ZEICHEN + 50);
});

test("Leiste: „Ganze Rückmeldung“ klappt den vollen Text auf und schließt bei anderem Text", async () => {
  const { readFileSync } = await import("node:fs");
  const js = readFileSync(new URL("./huelle.js", import.meta.url), "utf8");
  assert.ok(js.includes('leisteMehr: "Ganze Rückmeldung"') && js.includes('leisteWeniger: "Weniger anzeigen"'));
  // Offen ist der Kasten nur für genau den Text, für den er geöffnet wurde (Aufgabenwechsel, neue Bewertung: zu).
  assert.match(js, /const offenVoll = teile\.voll !== null && z\.leisteOffen === text;/);
  assert.match(js, /\$\("leisten-mehr"\)\.addEventListener\("click", \(\) => \{\s+z\.leisteOffen = z\.leisteOffen === z\.leistenText \? null : z\.leistenText;/);
  // Die Abfrage alle 2 s darf den Kasten nicht neu füllen (die Leseposition ginge verloren).
  assert.match(js, /if \(voll\.textContent !== inhalt\) voll\.textContent = inhalt;/);
});