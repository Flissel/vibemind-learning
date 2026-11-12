import test from "node:test";
import assert from "node:assert/strict";
import {
  BELEGT_KOPF, flaecheErneuern, flaecheSpeichern, flaechenAdresse, flaechenKennung, flaechenKopf, gruppeAusAdresse, KENNUNG_SCHLUESSEL,
  kennungBelegt, mitFlaechenKennung, nachrichtAusFlaeche, wiederholbar,
} from "./flaeche.js";

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
  assert.match(huelle, /let kennung = flaechenKennung\(\);/);
  // Fix-Runde 1: ohne data-src (fremde Seite, Test) bleibt der Rahmen unberührt.
  assert.match(huelle, /if \(rahmen\.dataset\.src\) rahmen\.src = flaechenAdresse\(rahmen\.dataset\.src, kennung\);/);
  assert.match(huelle, /headers: \{ \.\.\.flaechenKopf\(kennung\), \.\.\.\(body === undefined \? \{\} : \{ "content-type": "application\/json" \}\) \}/);
  assert.match(huelle, /belegt: kennungBelegt\(antwort\)/);
  // api() geht über mitFlaechenKennung: „belegt“ -> neue Kennung, Rahmen neu, höchstens eine Wiederholung.
  assert.match(huelle, /return mitFlaechenKennung\(\(\) => anfrage\(methode, pfad, body\), \{/);
  assert.match(huelle, /kennung = await flaecheErneuern\(\{ rahmen: \$\("rahmen"\), gruppe: z\.rahmenGruppe \}\);/);
  assert.match(huelle, /wiederholbar: wiederholbar\(methode, pfad\),/);
  assert.equal((huelle.match(/fetch\(/g) || []).length, 1); // nur anfrage() fragt die Brücke
  // Der Rahmen lädt erst mit Kennung (sonst meldete sich der Tab zuerst ohne sie beim MCP).
  assert.match(seite, /<iframe id="rahmen" data-src="<!--FLAECHE-->\/\?lernen=1" title="Fläche"><\/iframe>/);
  assert.doesNotMatch(seite, /<iframe id="rahmen" src=/);
});

// ---- Task 6, Fix-Runde 1 ---------------------------------------------------------------------------------------

test("Flächen-Adresse: relative Basis wird absolut, die Kennung steht immer in der URL", () => {
  const k = "lf-" + "a".repeat(32);
  assert.equal(flaechenAdresse("/?lernen=1", k, "https://lernen.example.test/lernen/"),
    `https://lernen.example.test/?lernen=1&flaeche=${k}`);
  assert.equal(flaechenAdresse("?lernen=1", k, "http://127.0.0.1:8151/"), `http://127.0.0.1:8151/?lernen=1&flaeche=${k}`);
});

test("Kennzeichen belegt: nur der Kopf X-Lernflaeche-Kennung: belegt", () => {
  const antwort = (wert) => ({ headers: { get: (n) => (n.toLowerCase() === "x-lernflaeche-kennung" ? wert : null) } });
  assert.equal(BELEGT_KOPF, "x-lernflaeche-kennung");
  assert.equal(kennungBelegt(antwort("belegt")), true);
  assert.equal(kennungBelegt(antwort(null)), false);
  assert.equal(kennungBelegt(antwort("anders")), false);
  assert.equal(kennungBelegt(null), false);
});

function rahmenAttrappe(src = `${FLAECHE}/?lernen=1`) {
  const hoerer = [];
  return {
    dataset: src === null ? {} : { src }, src: "", hoerer,
    addEventListener: (art, f) => { if (art === "load") hoerer.push(f); },
    removeEventListener: (art, f) => { const i = hoerer.indexOf(f); if (i >= 0) hoerer.splice(i, 1); },
  };
}

test("Fläche erneuern: neue Kennung in sessionStorage, Rahmen lädt mit ihr neu, wartet auf load", async () => {
  const alt = "lf-" + "a".repeat(32);
  const speicher = speicherAttrappe({ "lernflaeche-kennung": alt });
  const rahmen = rahmenAttrappe();
  const zufall = (feld) => feld.fill(0xcd);
  const warten = flaecheErneuern({ rahmen, speicher, zufall, wartenMs: 5000 });
  const neu = "lf-" + "cd".repeat(16);
  assert.equal(speicher.werte.get("lernflaeche-kennung"), neu); // auch wenn die alte gültig war
  assert.equal(rahmen.src, `${FLAECHE}/?lernen=1&flaeche=${neu}`);
  assert.equal(rahmen.hoerer.length, 1);
  rahmen.hoerer[0]();
  assert.equal(await warten, neu);
  assert.equal(rahmen.hoerer.length, 0);
  // Ohne load: nach der Wartezeit weiter. Ohne data-src: Rahmen unberührt.
  const still = rahmenAttrappe();
  assert.equal(await flaecheErneuern({ rahmen: still, speicher, zufall, wartenMs: 10 }), neu);
  const ohne = rahmenAttrappe(null);
  assert.equal(await flaecheErneuern({ rahmen: ohne, speicher, zufall, wartenMs: 10 }), neu);
  assert.equal(ohne.src, "");
});

test("mitFlaechenKennung: belegt -> erneuern und höchstens eine Wiederholung", async () => {
  const ablauf = async (antworten, wiederholbar) => {
    const gesendet = [];
    let erneuert = 0;
    const r = await mitFlaechenKennung(async () => { gesendet.push(1); return antworten.shift(); },
      { erneuern: async () => { erneuert += 1; }, wiederholbar });
    return { r, gesendet: gesendet.length, erneuert };
  };
  const ok = { ok: true, belegt: false, daten: { flaeche: true } };
  const belegt = { ok: true, belegt: true, daten: { flaeche: false } };
  assert.deepEqual(await ablauf([ok], true), { r: ok, gesendet: 1, erneuert: 0 });
  assert.deepEqual(await ablauf([belegt, ok], true), { r: ok, gesendet: 2, erneuert: 1 });
  // Scheitert es erneut: neue Kennung, aber keine zweite Wiederholung (die Seite zeigt „Fläche verbinden“).
  assert.deepEqual(await ablauf([belegt, belegt, ok], true), { r: belegt, gesendet: 2, erneuert: 2 });
  // Nicht wiederholbar (z. B. Antwort buchen): erneuern, nicht noch einmal senden.
  assert.deepEqual(await ablauf([belegt, ok], false), { r: belegt, gesendet: 1, erneuert: 1 });
});

test("wiederholbar: nur Anfragen, die nichts doppelt buchen", () => {
  for (const [m, p] of [["GET", "/kurse"], ["POST", "/kurse/k/oeffnen"], ["POST", "/sitzungen/s/weiter"], ["POST", "/sitzungen/s/zeigen"]]) {
    assert.equal(wiederholbar(m, p), true, p);
  }
  for (const p of ["/sitzungen/s/antwort", "/sitzungen/s/abgeben", "/sitzungen/s/passt", "/sitzungen/s/korrektur", "/kurse/k/neu"]) {
    assert.equal(wiederholbar("POST", p), false, p);
  }
});


// ---- Teil C, Task 8: Gruppenfläche --------------------------------------------------------------------------------

test("flaechenAdresse_mit_gruppe: &gruppe=g-<id>&person=<person> zusätzlich zur Kennung", () => {
  const k = "lf-" + "a".repeat(32);
  const person = "p" + "0123456789abcdef01234567";
  const gruppe = { gruppe: "g-7", person };
  assert.equal(flaechenAdresse(`${FLAECHE}/?lernen=1`, k, undefined, gruppe),
    `${FLAECHE}/?lernen=1&flaeche=${k}&gruppe=g-7&person=${person}`);
  // Ohne Gruppe genau wie bisher.
  assert.equal(flaechenAdresse(`${FLAECHE}/?lernen=1`, k, undefined, null), `${FLAECHE}/?lernen=1&flaeche=${k}`);
  // Ungültige Gruppe oder Person: keine Gruppenparameter (die Fläche bliebe sonst ohne passende Person).
  for (const falsch of [{ gruppe: "g-0", person }, { gruppe: "g-07", person }, { gruppe: "7", person },
    { gruppe: "g-1234567890", person }, { gruppe: "g-7", person: "p123" }, { gruppe: "g-7", person: "anna" },
    { gruppe: "g-7" }, "g-7"]) {
    assert.equal(flaechenAdresse(`${FLAECHE}/?lernen=1`, k, undefined, falsch), `${FLAECHE}/?lernen=1&flaeche=${k}`,
      JSON.stringify(falsch));
  }
  // Ohne gültige Kennung bleibt die Basis unverändert, auch mit Gruppe.
  assert.equal(flaechenAdresse(`${FLAECHE}/?lernen=1`, "x", undefined, gruppe), `${FLAECHE}/?lernen=1`);
});

test("gruppeAusAdresse: genau eine Gruppe aus 1 bis 9 Ziffern, sonst null", () => {
  assert.equal(gruppeAusAdresse("?kurs=k&gruppe=7"), 7);
  assert.equal(gruppeAusAdresse("?gruppe=123456789"), 123456789);
  for (const falsch of ["", "?kurs=k", "?gruppe=", "?gruppe=0", "?gruppe=07", "?gruppe=1234567890", "?gruppe=a",
    "?gruppe=1&gruppe=2", "?gruppe=-1"]) assert.equal(gruppeAusAdresse(falsch), null, falsch);
});

test("Fläche erneuern in der Gruppe: der Rahmen behält Gruppe und Person", async () => {
  const speicher = speicherAttrappe({});
  const rahmen = rahmenAttrappe();
  const gruppe = { gruppe: "g-7", person: "p" + "a".repeat(24) };
  const warten = flaecheErneuern({ rahmen, speicher, zufall: (f) => f.fill(0xcd), wartenMs: 10, gruppe });
  const neu = "lf-" + "cd".repeat(16);
  assert.equal(rahmen.src, `${FLAECHE}/?lernen=1&flaeche=${neu}&gruppe=g-7&person=${gruppe.person}`);
  assert.equal(await warten, neu);
});

test("huelle.js: Gruppenroute, Person aus der Antwort, Kopf „Gruppe: …“, Meldung aus der Antwort", async () => {
  const { readFile } = await import("node:fs/promises");
  const huelle = await readFile(new URL("./huelle.js", import.meta.url), "utf8");
  assert.match(huelle, /const gruppeId = gruppeAusAdresse\(location\.search\);/);
  assert.match(huelle, /`\/gruppen\/\$\{z\.gruppeId\}\/oeffnen`/);
  assert.match(huelle, /`Gruppe: \$\{/);
  // Die Person im Rahmen kommt nur aus der Antwort der Brücke (sonst wären die eigenen Kacheln „fremd“).
  assert.match(huelle, /person: g\.person/);
  assert.match(huelle, /kennung = await flaecheErneuern\(\{ rahmen: \$\("rahmen"\), gruppe: z\.rahmenGruppe \}\);/);
});
// ---- Nachrichten der Flaeche an die Huelle -------------------------------------------------------
test("nachrichtAusFlaeche: gueltige Nachrichten werden normalisiert", () => {
  const quelle = {};
  const e = (data, o = {}) => ({ source: quelle, origin: FLAECHE, data, ...o });
  assert.deepEqual(nachrichtAusFlaeche(e({ typ: "huelle-abgeben", bereich: 2, extra: 1 }), quelle, FLAECHE), { typ: "huelle-abgeben", bereich: 2 });
  assert.deepEqual(nachrichtAusFlaeche(e({ typ: "huelle-ki-schritt", bereich: 0, aktion: "frage", ausloeser: "auto" }), quelle, FLAECHE),
    { typ: "huelle-ki-schritt", bereich: 0, aktion: "frage", ausloeser: "auto" });
  assert.deepEqual(nachrichtAusFlaeche(e({ typ: "huelle-mcp-bereit" }), quelle, FLAECHE), { typ: "huelle-mcp-bereit" });
});

test("nachrichtAusFlaeche: falsche Herkunft, Quelle, Typ oder Bereich ergeben null", () => {
  const quelle = {};
  const gut = { typ: "huelle-abgeben", bereich: 1 };
  assert.equal(nachrichtAusFlaeche({ source: quelle, origin: "https://boese.example.test", data: gut }, quelle, FLAECHE), null);
  assert.equal(nachrichtAusFlaeche({ source: {}, origin: FLAECHE, data: gut }, quelle, FLAECHE), null);
  assert.equal(nachrichtAusFlaeche({ source: quelle, origin: FLAECHE, data: gut }, null, FLAECHE), null);
  assert.equal(nachrichtAusFlaeche({ source: quelle, origin: FLAECHE, data: gut }, quelle, null), null);
  for (const data of [null, "x", { typ: "unbekannt", bereich: 1 }, { typ: "huelle-speichern", id: "a" }]) {
    assert.equal(nachrichtAusFlaeche({ source: quelle, origin: FLAECHE, data }, quelle, FLAECHE), null);
  }
  for (const bereich of [-1, 1.5, "2", null, undefined, NaN, 500]) {
    assert.equal(nachrichtAusFlaeche({ source: quelle, origin: FLAECHE, data: { typ: "huelle-abgeben", bereich } }, quelle, FLAECHE), null, String(bereich));
    assert.equal(nachrichtAusFlaeche({ source: quelle, origin: FLAECHE, data: { typ: "huelle-ki-schritt", bereich, aktion: "a", ausloeser: "auto" } }, quelle, FLAECHE), null);
  }
});

test("nachrichtAusFlaeche: ki-schritt braucht aktion (String) und bekannten Ausloeser", () => {
  const quelle = {};
  const e = (data) => ({ source: quelle, origin: FLAECHE, data });
  assert.equal(nachrichtAusFlaeche(e({ typ: "huelle-ki-schritt", bereich: 1, aktion: "frage", ausloeser: "sonst" }), quelle, FLAECHE), null);
  assert.equal(nachrichtAusFlaeche(e({ typ: "huelle-ki-schritt", bereich: 1, aktion: 5, ausloeser: "manuell" }), quelle, FLAECHE), null);
  assert.equal(nachrichtAusFlaeche(e({ typ: "huelle-ki-schritt", bereich: 1, ausloeser: "manuell" }), quelle, FLAECHE), null);
});

test("huelle.js verdrahtet die Nachrichten der Flaeche (Quelltext)", async () => {
  const { readFileSync } = await import("node:fs");
  const js = readFileSync(new URL("./huelle.js", import.meta.url), "utf8");
  assert.match(js, /addEventListener\("message", flaechenNachricht\)/);
  assert.match(js, /nachrichtAusFlaeche\(ereignis, rahmen\.contentWindow, flaechenHerkunft\(rahmen\)\)/);
  assert.match(js, /abgabeNummerZulaessig\(nummer, abgabeZustand\(\)\)/);
  assert.match(js, /await abgeben\(\)/); // dieselbe Funktion wie der Knopf
  assert.match(js, /\/ki-schritt`/);
  assert.match(js, /m\.bereich \+ 1/);
  assert.match(js, /mcpAutoVerbunden/);
  assert.match(js, /autoVerbindenFaellig\(/);
  assert.match(js, /z\.turn\?\.session_id !== t\.session_id/);
});

test("nachrichtAusFlaeche: gültiges dokument wird übernommen, ungültiges weggelassen", () => {
  const quelle = {};
  const e = (data) => ({ source: quelle, origin: FLAECHE, data });
  assert.deepEqual(nachrichtAusFlaeche(e({ typ: "huelle-abgeben", bereich: 0, dokument: "doc-1" }), quelle, FLAECHE),
    { typ: "huelle-abgeben", bereich: 0, dokument: "doc-1" });
  assert.deepEqual(nachrichtAusFlaeche(e({ typ: "huelle-abgeben", bereich: 0, dokument: "<x>" }), quelle, FLAECHE),
    { typ: "huelle-abgeben", bereich: 0 });
  assert.deepEqual(nachrichtAusFlaeche(e({ typ: "huelle-ki-schritt", bereich: 1, aktion: "a", ausloeser: "auto", dokument: "doc-2" }), quelle, FLAECHE),
    { typ: "huelle-ki-schritt", bereich: 1, aktion: "a", ausloeser: "auto", dokument: "doc-2" });
  assert.deepEqual(nachrichtAusFlaeche(e({ typ: "huelle-ki-schritt", bereich: 1, aktion: "a", ausloeser: "auto", dokument: 5 }), quelle, FLAECHE),
    { typ: "huelle-ki-schritt", bereich: 1, aktion: "a", ausloeser: "auto" });
});

test("nachrichtAusFlaeche: huelle-dokument-aktiv nur mit gültigem dokument", () => {
  const quelle = {};
  const e = (data) => ({ source: quelle, origin: FLAECHE, data });
  assert.equal(nachrichtAusFlaeche(e({ typ: "huelle-dokument-aktiv" }), quelle, FLAECHE), null);
  assert.equal(nachrichtAusFlaeche(e({ typ: "huelle-dokument-aktiv", dokument: "a b" }), quelle, FLAECHE), null);
  assert.equal(nachrichtAusFlaeche(e({ typ: "huelle-dokument-aktiv", dokument: "x".repeat(257) }), quelle, FLAECHE), null);
  assert.deepEqual(nachrichtAusFlaeche(e({ typ: "huelle-dokument-aktiv", dokument: "doc-1", extra: 1 }), quelle, FLAECHE),
    { typ: "huelle-dokument-aktiv", dokument: "doc-1" });
});
