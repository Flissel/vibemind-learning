import test from "node:test";
import assert from "node:assert/strict";
import { flaecheSpeichern } from "./flaeche.js";

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
