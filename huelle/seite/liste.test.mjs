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
  assert.equal(naechsteOffene(liste, 4), 2);
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
