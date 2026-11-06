import test from "node:test";
import assert from "node:assert/strict";
import {
  ANFRAGE, ANTWORT, anfrageAus, herkuenfteAus, installieren, speichernAusloesen, zustandBewerten,
} from "./speichern.js";

const SEITE = "https://lernen.example.test";
const ELTERN = { name: "eltern" };

// Nachbau der zwei PenEcho-Elemente (public/index.html, app.js updateStudioDocumentState).
function penecho({ start = "unsaved", nachKlick = ["saving", "saved"], gesperrt = false, ohne = [] } = {}) {
  const anzeige = { dataset: { state: start } };
  let schritte = [];
  const knopf = {
    disabled: gesperrt,
    klicks: 0,
    click() {
      if (this.disabled) return;
      this.klicks += 1;
      schritte = [...nachKlick];
      anzeige.dataset.state = schritte.shift() ?? anzeige.dataset.state; // setHistorySaveBusy(true) laeuft synchron
    },
  };
  const dok = {
    querySelector(sel) {
      if (sel === "#saveCanvasBtn") return ohne.includes("knopf") ? null : knopf;
      if (sel === "#canvasDocumentSaveState") return ohne.includes("anzeige") ? null : anzeige;
      return null;
    },
  };
  // Jede Pause laesst PenEcho einen Schritt weiter.
  let zeit = 0;
  const optionen = {
    zeitlimitMs: 1000,
    jetzt: () => zeit,
    schlaf: async (ms) => {
      zeit += ms;
      if (schritte.length) anzeige.dataset.state = schritte.shift();
    },
  };
  return { dok, knopf, anzeige, optionen };
}

test("herkuenfteAus: nur saubere Herkuenfte, kein *", () => {
  assert.deepEqual(herkuenfteAus(`${SEITE} http://127.0.0.1:8150`), [SEITE, "http://127.0.0.1:8150"]);
  assert.deepEqual(herkuenfteAus("* null https://a.test/pfad  kaputt"), []);
  assert.deepEqual(herkuenfteAus(null), []);
});

test("anfrageAus: nur erlaubte Herkunft, nur das Elternfenster, nur gueltige Nachricht", () => {
  const gut = { origin: SEITE, source: ELTERN, data: { typ: ANFRAGE, id: "a1" } };
  assert.deepEqual(anfrageAus(gut, [SEITE], ELTERN), { id: "a1", origin: SEITE });
  assert.equal(anfrageAus({ ...gut, origin: "https://boese.test" }, [SEITE], ELTERN), null);
  assert.equal(anfrageAus({ ...gut, origin: "null" }, [SEITE], ELTERN), null);
  assert.equal(anfrageAus({ ...gut, source: { name: "anderes" } }, [SEITE], ELTERN), null);
  assert.equal(anfrageAus({ ...gut, data: { typ: "etwas", id: "a1" } }, [SEITE], ELTERN), null);
  assert.equal(anfrageAus({ ...gut, data: { typ: ANFRAGE } }, [SEITE], ELTERN), null);
  assert.equal(anfrageAus({ ...gut, data: { typ: ANFRAGE, id: "x".repeat(200) } }, [SEITE], ELTERN), null);
  assert.equal(anfrageAus({ ...gut, data: "huelle-speichern" }, [SEITE], ELTERN), null);
  assert.equal(anfrageAus(gut, [], ELTERN), null);
});

test("zustandBewerten: saving laeuft, saved ok, sonst ein Grund", () => {
  assert.equal(zustandBewerten("saving"), null);
  assert.deepEqual(zustandBewerten("saved"), { ok: true, grund: null });
  for (const z of ["unsaved", "edited", undefined, "neu"]) {
    const e = zustandBewerten(z);
    assert.equal(e.ok, false);
    assert.ok(e.grund.length > 0, z);
  }
});

test("speichernAusloesen: klickt PenEchos Knopf und wartet auf Saved", async () => {
  const p = penecho();
  assert.deepEqual(await speichernAusloesen(p.dok, p.optionen), { ok: true, grund: null });
  assert.equal(p.knopf.klicks, 1);
});

test("speichernAusloesen: auch wenn schon gespeichert war (ueberschreiben)", async () => {
  const p = penecho({ start: "saved" });
  assert.deepEqual(await speichernAusloesen(p.dok, p.optionen), { ok: true, grund: null });
});

test("speichernAusloesen: PenEcho speichert nichts (leere Flaeche) -> ok false", async () => {
  const p = penecho({ nachKlick: ["saving", "unsaved"] });
  const e = await speichernAusloesen(p.dok, p.optionen);
  assert.equal(e.ok, false);
  assert.match(e.grund, /nicht gespeichert/);
});

test("speichernAusloesen: Klick ohne Wirkung -> ok false, nicht still", async () => {
  const p = penecho({ nachKlick: [] });
  const e = await speichernAusloesen(p.dok, p.optionen);
  assert.equal(e.ok, false);
  assert.match(e.grund, /nicht begonnen/);
});

test("speichernAusloesen: Zeitlimit", async () => {
  const p = penecho({ nachKlick: Array(50).fill("saving") });
  const e = await speichernAusloesen(p.dok, p.optionen);
  assert.equal(e.ok, false);
  assert.match(e.grund, /Zeitlimit/);
});

test("speichernAusloesen: Knopf oder Anzeige fehlt (PenEcho geaendert) -> klarer Grund", async () => {
  const ohneKnopf = penecho({ ohne: ["knopf"] });
  assert.deepEqual(await speichernAusloesen(ohneKnopf.dok, ohneKnopf.optionen),
    { ok: false, grund: "PenEcho-Knopf #saveCanvasBtn fehlt" });
  const ohneAnzeige = penecho({ ohne: ["anzeige"] });
  assert.deepEqual(await speichernAusloesen(ohneAnzeige.dok, ohneAnzeige.optionen),
    { ok: false, grund: "PenEcho-Anzeige #canvasDocumentSaveState fehlt" });
});

test("speichernAusloesen: gesperrter Knopf -> ok false", async () => {
  const p = penecho({ gesperrt: true });
  const e = await speichernAusloesen(p.dok, p.optionen);
  assert.equal(e.ok, false);
  assert.match(e.grund, /gesperrt/);
  assert.equal(p.knopf.klicks, 0);
});

test("speichernAusloesen: laeuft schon ein Speichern, wird nicht erneut geklickt", async () => {
  const p = penecho({ start: "saving", gesperrt: true });
  let schritt = 0;
  p.optionen.schlaf = async () => { schritt += 1; if (schritt === 2) p.anzeige.dataset.state = "saved"; };
  assert.deepEqual(await speichernAusloesen(p.dok, p.optionen), { ok: true, grund: null });
  assert.equal(p.knopf.klicks, 0);
});

function fenster(dok) {
  const hoerer = [];
  return {
    document: dok,
    parent: ELTERN,
    addEventListener: (art, f) => { if (art === "message") hoerer.push(f); },
    removeEventListener: (art, f) => { const i = hoerer.indexOf(f); if (i >= 0) hoerer.splice(i, 1); },
    async senden(ereignis) { await Promise.all(hoerer.map((f) => f(ereignis))); },
    hoerer,
  };
}

test("installieren: beantwortet eine erlaubte Anfrage an die anfragende Herkunft", async () => {
  const p = penecho();
  const f = fenster(p.dok);
  const post = [];
  const quelle = { ...ELTERN, postMessage: (d, o) => post.push([d, o]) };
  f.parent = quelle;
  installieren(f, [SEITE], p.optionen);
  await f.senden({ origin: SEITE, source: quelle, data: { typ: ANFRAGE, id: "r1" } });
  assert.deepEqual(post, [[{ typ: ANTWORT, id: "r1", ok: true, grund: null }, SEITE]]);
});

test("installieren: fremde Herkunft bekommt keine Antwort und loest nichts aus", async () => {
  const p = penecho();
  const f = fenster(p.dok);
  const post = [];
  const quelle = { ...ELTERN, postMessage: (d, o) => post.push([d, o]) };
  f.parent = quelle;
  installieren(f, [SEITE], p.optionen);
  await f.senden({ origin: "https://boese.test", source: quelle, data: { typ: ANFRAGE, id: "r1" } });
  assert.deepEqual(post, []);
  assert.equal(p.knopf.klicks, 0);
});

test("installieren: Fehler im Ausloesen wird als ok false beantwortet", async () => {
  const f = fenster({ querySelector() { throw new Error("kaputt"); } });
  const post = [];
  const quelle = { postMessage: (d, o) => post.push([d, o]) };
  f.parent = quelle;
  installieren(f, [SEITE], {});
  await f.senden({ origin: SEITE, source: quelle, data: { typ: ANFRAGE, id: "r2" } });
  assert.equal(post.length, 1);
  assert.equal(post[0][0].ok, false);
  assert.match(post[0][0].grund, /kaputt/);
});

test("installieren: liefert eine Abmeldung", () => {
  const f = fenster(penecho().dok);
  const ab = installieren(f, [SEITE], {});
  assert.equal(f.hoerer.length, 1);
  ab();
  assert.equal(f.hoerer.length, 0);
});
