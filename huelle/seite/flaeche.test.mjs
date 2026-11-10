import test from "node:test";
import assert from "node:assert/strict";
import { flaecheSpeichern, flaechenAdresse, flaechenKennung, flaechenKopf, KENNUNG_SCHLUESSEL } from "./flaeche.js";

const FLAECHE = "https://flaeche.example.test";

function aufbau({ antwort } = {}) {
  const hoerer = [];
  const fenster = {
    addEventListener: (art, f) => { if (art === "message") hoerer.push(f); },
    removeEventListener: (art, f) => { const i = hoerer.indexOf(f); if (i >= 0) hoerer.splice(i, 1); },
  };
  const gesendet = [];
  const kind = {
    postMessage(daten, ziel) {
      gesendet.push([daten, ziel]);
      if (antwort) queueMicrotask(() => antwort(daten, (e) => hoerer.slice().forEach((f) => f(e)), kind));
    },
  };
  const iframe = { src: `${FLAECHE}/`, contentWindow: kind };
  return { fenster, iframe, gesendet, hoerer, kind };
}

test("schickt die Anfrage an die Herkunft der Flaeche und liefert die passende Antwort", async () => {
  const a = aufbau({
    antwort: (d, senden, kind) => senden({ origin: FLAECHE, source: kind, data: { typ: "huelle-gespeichert", id: d.id, ok: true, grund: null } }),
  });
  const e = await flaecheSpeichern(a.iframe, { fenster: a.fenster, neueId: () => "id-1", zeitlimitMs: 500 });
  assert.deepEqual(e, { ok: true, grund: null, code: "gespeichert" });
  assert.deepEqual(a.gesendet, [[{ typ: "huelle-speichern", id: "id-1" }, FLAECHE]]);
  assert.equal(a.hoerer.length, 0); // aufgeraeumt
});

test("ok false mit dem Grund der Flaeche", async () => {
  const a = aufbau({
    antwort: (d, senden, kind) => senden({ origin: FLAECHE, source: kind, data: { typ: "huelle-gespeichert", id: d.id, ok: false, grund: "PenEcho-Knopf #saveCanvasBtn fehlt", code: "kein_knopf" } }),
  });
  const e = await flaecheSpeichern(a.iframe, { fenster: a.fenster, zeitlimitMs: 500 });
  assert.deepEqual(e, { ok: false, grund: "PenEcho-Knopf #saveCanvasBtn fehlt", code: "kein_knopf" });
});

test("fremde Herkunft, fremde Quelle oder andere id zaehlen nicht; dann Zeitlimit", async () => {
  const a = aufbau({
    antwort: (d, senden, kind) => {
      senden({ origin: "https://boese.test", source: kind, data: { typ: "huelle-gespeichert", id: d.id, ok: true } });
      senden({ origin: FLAECHE, source: {}, data: { typ: "huelle-gespeichert", id: d.id, ok: true } });
      senden({ origin: FLAECHE, source: kind, data: { typ: "huelle-gespeichert", id: "andere", ok: true } });
      senden({ origin: FLAECHE, source: kind, data: "huelle-gespeichert" });
    },
  });
  const e = await flaecheSpeichern(a.iframe, { fenster: a.fenster, zeitlimitMs: 50 });
  assert.deepEqual(e, { ok: false, grund: "keine Antwort", code: "keine_antwort" });
  assert.equal(a.hoerer.length, 0);
});

test("keine Antwort: ok false nach dem Zeitlimit", async () => {
  const a = aufbau();
  const e = await flaecheSpeichern(a.iframe, { fenster: a.fenster, zeitlimitMs: 30 });
  assert.deepEqual(e, { ok: false, grund: "keine Antwort", code: "keine_antwort" });
});

test("ok nur bei ok === true", async () => {
  const a = aufbau({
    antwort: (d, senden, kind) => senden({ origin: FLAECHE, source: kind, data: { typ: "huelle-gespeichert", id: d.id, ok: "ja" } }),
  });
  const e = await flaecheSpeichern(a.iframe, { fenster: a.fenster, zeitlimitMs: 500 });
  assert.equal(e.ok, false);
  assert.ok(e.grund);
  assert.equal(e.code, "fehler");
});

test("unbekannter oder gefaelschter code wird zu fehler, bekannter bleibt", async () => {
  for (const [roh, erwartet] of [["geaendert", "geaendert"], ["leer", "leer"], ["gespeichert", "fehler"], ["quatsch", "fehler"], [undefined, "fehler"]]) {
    const a = aufbau({
      antwort: (d, senden, kind) => senden({ origin: FLAECHE, source: kind, data: { typ: "huelle-gespeichert", id: d.id, ok: false, grund: "x", code: roh } }),
    });
    assert.equal((await flaecheSpeichern(a.iframe, { fenster: a.fenster, zeitlimitMs: 500 })).code, erwartet, String(roh));
  }
});

test("ohne Flaeche: ok false, nichts gesendet", async () => {
  const a = aufbau();
  assert.deepEqual(await flaecheSpeichern({ src: "", contentWindow: a.kind }, { fenster: a.fenster }),
    { ok: false, grund: "keine Fläche", code: "keine_flaeche" });
  assert.deepEqual(await flaecheSpeichern({ src: `${FLAECHE}/`, contentWindow: null }, { fenster: a.fenster }),
    { ok: false, grund: "keine Fläche", code: "keine_flaeche" });
  assert.deepEqual(await flaecheSpeichern(null, { fenster: a.fenster }), { ok: false, grund: "keine Fläche", code: "keine_flaeche" });
  assert.deepEqual(a.gesendet, []);
});

// ---- Teil B, Task 6: eigene Fläche je Tab ----------------------------------------------------------------

function speicherAttrappe(start = {}) {
  const werte = new Map(Object.entries(start));
  return { werte, getItem: (k) => (werte.has(k) ? werte.get(k) : null), setItem: (k, v) => werte.set(k, String(v)) };
}

test("Flächen-Kennung: je Tab aus crypto.getRandomValues, liegt in sessionStorage unter lernflaeche-kennung", () => {
  assert.equal(KENNUNG_SCHLUESSEL, "lernflaeche-kennung");
  const speicher = speicherAttrappe();
  const groessen = [];
  const zufall = (feld) => { groessen.push(feld.length); feld.set(Array.from({ length: feld.length }, (_, i) => i * 17)); return feld; };
  const kennung = flaechenKennung({ speicher, zufall });
  assert.match(kennung, /^lf-[0-9a-f]{32}$/);
  assert.equal(kennung, "lf-00112233445566778899aabbccddeeff");
  assert.deepEqual(groessen, [16]); // 16 Byte -> 32 hex
  assert.equal(speicher.werte.get("lernflaeche-kennung"), kennung);
  // Neu laden im selben Tab: dieselbe Kennung, kein neuer Zufall.
  assert.equal(flaechenKennung({ speicher, zufall: () => assert.fail("kein neuer Zufall") }), kennung);
});

test("Flächen-Kennung: ungültiger gespeicherter Wert wird ersetzt, ohne Speicher trotzdem eine Kennung", () => {
  const zufall = (feld) => feld.fill(255);
  for (const alt of ["", "lf-xyz", "lf-" + "A".repeat(32), "lf-" + "a".repeat(33)]) {
    const speicher = speicherAttrappe({ "lernflaeche-kennung": alt });
    assert.equal(flaechenKennung({ speicher, zufall }), "lf-" + "f".repeat(32), alt);
    assert.equal(speicher.werte.get("lernflaeche-kennung"), "lf-" + "f".repeat(32));
  }
  const kaputt = { getItem: () => { throw new Error("gesperrt"); }, setItem: () => { throw new Error("gesperrt"); } };
  assert.equal(flaechenKennung({ speicher: kaputt, zufall }), "lf-" + "f".repeat(32));
  assert.equal(flaechenKennung({ speicher: null, zufall }), "lf-" + "f".repeat(32));
});

test("Flächen-Adresse: die Kennung geht als &flaeche= in die iframe-URL", () => {
  const k = "lf-" + "a".repeat(32);
  assert.equal(flaechenAdresse(`${FLAECHE}/?lernen=1`, k), `${FLAECHE}/?lernen=1&flaeche=${k}`);
  assert.equal(flaechenAdresse("http://127.0.0.1:8151/?lernen=1", k), `http://127.0.0.1:8151/?lernen=1&flaeche=${k}`);
  // Ungültige Kennung oder Adresse: unverändert (dann wie bisher, ohne eigene Fläche).
  assert.equal(flaechenAdresse(`${FLAECHE}/?lernen=1`, "x"), `${FLAECHE}/?lernen=1`);
  assert.equal(flaechenAdresse("kaputt", k), "kaputt");
});

test("Flächen-Kopf: X-Lernflaeche mit der Kennung, ohne gültige Kennung kein Kopf", () => {
  const k = "lf-" + "b".repeat(32);
  assert.deepEqual(flaechenKopf(k), { "X-Lernflaeche": k });
  assert.deepEqual(flaechenKopf("x"), {});
  assert.deepEqual(flaechenKopf(null), {});
});

test("huelle.js setzt die iframe-URL und schickt den Kopf an jede API-Anfrage", async () => {
  const { readFile } = await import("node:fs/promises");
  const huelle = await readFile(new URL("./huelle.js", import.meta.url), "utf8");
  const seite = await readFile(new URL("./index.html", import.meta.url), "utf8");
  assert.match(huelle, /const KENNUNG = flaechenKennung\(\);/);
  assert.match(huelle, /rahmen\.src = flaechenAdresse\(rahmen\.dataset\.src, KENNUNG\);/);
  assert.match(huelle, /headers: \{ \.\.\.flaechenKopf\(KENNUNG\), \.\.\.\(body === undefined \? \{\} : \{ "content-type": "application\/json" \}\) \}/);
  assert.equal((huelle.match(/fetch\(/g) || []).length, 1); // nur api() fragt die Brücke
  // Der Rahmen lädt erst mit Kennung (sonst meldete sich der Tab zuerst ohne sie beim MCP).
  assert.match(seite, /<iframe id="rahmen" data-src="<!--FLAECHE-->\/\?lernen=1" title="Fläche"><\/iframe>/);
  assert.doesNotMatch(seite, /<iframe id="rahmen" src=/);
});
