import test from "node:test";
import assert from "node:assert/strict";
import { LISTEN_MELDUNGEN, mussAbfragen, naechsteOffene, statusText, warteText } from "./liste.js";

const b = { status: "bestaetigt" };
const o = { status: "offen" };

test("naechsteOffene: erste nicht bestätigte nach ab, am Ende von vorn", () => {
  assert.equal(naechsteOffene([b, o, b, o], 2), 4);
  assert.equal(naechsteOffene([b, o, b, b], 3), 2);
  assert.equal(naechsteOffene([b, b], 1), null);
});

test("naechsteOffene: nimmt die Nummern der Einträge, jeder andere Status zählt als offen", () => {
  const liste = [
    { nummer: 1, status: "bestaetigt" }, { nummer: 2, status: "in_bewertung" },
    { nummer: 3, status: "bestaetigt" }, { nummer: 4, status: "fehler" },
  ];
  assert.equal(naechsteOffene(liste, 2), 4);
  // Aufgaben in Bewertung nur als letzte Wahl (Abnahme A2, Befund 3).
  assert.equal(naechsteOffene(liste, 4), 4);
  assert.equal(naechsteOffene([b, { nummer: 2, status: "in_bewertung" }, b], 1), 2);
  assert.equal(naechsteOffene([b, { status: "in_bewertung" }, b, o], 1), 4);
  assert.equal(naechsteOffene([], 1), null);
  assert.equal(naechsteOffene(null, 1), null);
});

test("warteText", () => {
  assert.equal(warteText(0), "");
  assert.equal(warteText(1), "1 Abgabe wird bewertet");
  assert.equal(warteText(3), "3 Abgaben werden bewertet");
  assert.equal(warteText(undefined), "");
});

test("statusText je Status, fehler mit der Meldung", () => {
  assert.equal(statusText("rueckmeldung"), "Rückmeldung da");
  assert.equal(statusText("offen"), "offen");
  assert.equal(statusText("in_bewertung"), "wird bewertet …");
  assert.equal(statusText("bestaetigt"), "erledigt");
  const meldung = "Die Bewertung ist gerade nicht erreichbar. Bitte geben Sie gleich noch einmal ab.";
  assert.equal(statusText("fehler", meldung), meldung);
  assert.equal(statusText("fehler"), "");
  assert.equal(statusText("unbekannt"), "");
});

test("mussAbfragen nur, solange eine Aufgabe bewertet wird", () => {
  assert.equal(mussAbfragen([o, { status: "in_bewertung" }]), true);
  assert.equal(mussAbfragen([o, b, { status: "rueckmeldung" }, { status: "fehler" }]), false);
  assert.equal(mussAbfragen(undefined), false);
});

test("die beiden neuen Meldungen der Brücke sind bekannt", () => {
  assert.ok(LISTEN_MELDUNGEN.has("Diese Aufgabe wird gerade bewertet."));
  assert.ok(LISTEN_MELDUNGEN.has("Diese Aufgabe ist schon bestätigt."));
});


import { eintragSetzen, standAnnehmen } from "./liste.js";

const STAND = {
  ablauf: "liste", aktuell: 1, wartend: 0,
  aufgaben: [
    { nummer: 1, status: "offen", meldung: null }, { nummer: 2, status: "offen", meldung: null },
    { nummer: 3, status: "bestaetigt", meldung: null },
  ],
};

test("standAnnehmen: eine Antwort, deren Anfrage vor der letzten Abgabe begann, gilt nicht", () => {
  assert.equal(standAnnehmen(STAND, { anfrage: 4, gueltigAb: 5, sendend: new Set() }), null);
  const neu = standAnnehmen(STAND, { anfrage: 6, gueltigAb: 5, sendend: new Set() });
  assert.deepEqual(neu, { aktuell: 1, wartend: 0, aufgaben: STAND.aufgaben });
  // Die eigene Antwort einer Abgabe (anfrage Infinity) gilt immer.
  assert.ok(standAnnehmen(STAND, { anfrage: Infinity, gueltigAb: 9, sendend: new Set() }));
});

test("standAnnehmen: Nummern, deren Abgabe noch unterwegs ist, bleiben „in_bewertung“ (nicht bestätigte)", () => {
  const neu = standAnnehmen(STAND, { anfrage: 6, gueltigAb: 5, sendend: new Set([2, 3]) });
  assert.deepEqual(neu.aufgaben.map((a) => a.status), ["offen", "in_bewertung", "bestaetigt"]);
  assert.equal(STAND.aufgaben[1].status, "offen"); // die Eingabe bleibt unverändert
});

test("standAnnehmen: kein Listenstand -> null", () => {
  for (const kaputt of [null, {}, { ablauf: "liste", aufgaben: [] }, { ablauf: "anpassend", aufgaben: STAND.aufgaben }]) {
    assert.equal(standAnnehmen(kaputt, { anfrage: 1, gueltigAb: 0, sendend: new Set() }), null);
  }
});

test("eintragSetzen: ändert den Eintrag mit dieser Nummer in einer neuen Liste", () => {
  const liste = { aktuell: 1, wartend: 0, aufgaben: STAND.aufgaben };
  const neu = eintragSetzen(liste, 2, { status: "in_bewertung", meldung: null });
  assert.deepEqual(neu.aufgaben.map((a) => a.status), ["offen", "in_bewertung", "bestaetigt"]);
  assert.equal(liste.aufgaben[1].status, "offen");
  assert.equal(eintragSetzen(liste, 9, { status: "offen" }), liste); // unbekannte Nummer: unverändert
  assert.equal(eintragSetzen(null, 1, { status: "offen" }), null);
});
import { listenErgebnisAus, zusammenfassungSessionId } from "./liste.js";

test("listenErgebnisAus: eine Karte, die nicht auf der Fläche steht, kommt als karte mit", () => {
  const a = { nummer: 2, status: "rueckmeldung", urteil: "Richtig.", vorbewertung_id: "v-2", karte: "Gelesen: 2 – Richtig." };
  assert.deepEqual(listenErgebnisAus(a, new Set()), {
    vorbewertung_id: "v-2", urteil: "Richtig.", lesbar: true, karte: "Gelesen: 2 – Richtig.",
  });
  const ohne = listenErgebnisAus({ ...a, karte: undefined }, new Set());
  assert.ok(ohne && !("karte" in ohne));
  assert.equal(listenErgebnisAus({ ...a, status: "offen" }, new Set()), null);
  assert.equal(listenErgebnisAus(a, new Set(["v-2"])), null);
  assert.equal(listenErgebnisAus({ ...a, urteil: "Nicht lesbar." }, new Set()).lesbar, false);
});

test("zusammenfassungSessionId: auch ein beendeter Listenkurs hat den Link, ein anpassender nicht", () => {
  const turn = { session_id: "abc" };
  assert.equal(zusammenfassungSessionId({ aufgaben: [] }, false, turn), "abc");
  assert.equal(zusammenfassungSessionId(null, true, turn), "abc");
  assert.equal(zusammenfassungSessionId(null, false, turn), null);
  assert.equal(zusammenfassungSessionId(null, true, { session_id: "" }), null);
  assert.equal(zusammenfassungSessionId(null, true, null), null);
});

test("mussAbfragen auch, solange eine Karte noch auf die Fläche kommt (karte_ausstehend)", () => {
  assert.equal(mussAbfragen([o, { status: "rueckmeldung", karte_ausstehend: true }]), true);
  assert.equal(mussAbfragen([o, { status: "rueckmeldung", karte: "Gelesen: 2" }]), false);
  assert.equal(mussAbfragen([o, { status: "rueckmeldung", karte_ausstehend: false }]), false);
});
