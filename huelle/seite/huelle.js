// Bedienteil der Lern-Huelle: Aufgabenkarte neben der PenEcho-Flaeche.
import {
  antwortAus, begriffeZusammenfassen, begriffTitel, eingabeHinweis, ergebnisAus, ergebnisHinweis, fehlerText,
  flaechenHinweis, FLAECHEN_HINWEIS, formel, klartext, KNOPF_TEXT, korrekturAntwort, leistenZustand, meldungFuer,
  nach409, speicherAusgang, speichernMitWiederholung, sperrHinweis, SPERR_HINWEIS,
} from "./aufgabe.js";
import { flaecheSpeichern } from "./flaeche.js";

const API = "/lernen/api";

// Feste Texte (Plan, global.md).
const TEXT = {
  nichtErreichbar: "Learning ist gerade nicht erreichbar. Ihre Fläche bleibt erhalten.",
  schonAbgegeben: "Diese Aufgabe wurde schon abgegeben. Ich lade den aktuellen Stand.",
  keinKurs: "Dieser Kurs hat noch keine freigegebenen Aufgaben.",
  flaecheWeg: "Die Fläche ist gerade nicht erreichbar.",
  sperre: SPERR_HINWEIS,
  nichtAngenommen: "Diese Abgabe wurde nicht angenommen. Bitte prüfen Sie Ihre Eingabe.",
  wirdGeprueft: "Ihre Abgabe wird noch geprüft. Sie zählt, sobald die Prüfung fertig ist.",
  keinHinweis: "Kein Hinweis verfügbar.",
  keinHinweisPruefung: "Im Prüfungsmodus gibt es keine Hinweise.",
  allgemein: "Das hat gerade nicht geklappt. Bitte versuchen Sie es noch einmal.",
  bewertungFehler: "Die Bewertung konnte nicht gespeichert werden. Bitte versuchen Sie es noch einmal.",
  rechnen: "Sie können auf der Fläche rechnen oder zeichnen.",
  aufFlaeche: "Lösen Sie die Aufgabe auf der Fläche. „Abgeben“ finden Sie unten rechts.",
  ueberarbeiten: "Ändern Sie Ihre Antwort auf der Fläche und drücken Sie dann erneut „Abgeben“.",
  nichtZugeordnet: "Das konnte ich nicht zuordnen.",
  wirdBewertet: "Wird bewertet …",
};
const BEWERTET_NACH_MS = 4000; // Passt wartet höchstens so lange auf das Speichern, dann geht es weiter
const SMILEYS = [
  { wert: "schlecht", zeichen: "😕" },
  { wert: "mittel", zeichen: "🙂" },
  { wert: "gut", zeichen: "😃" },
];
const MAX_STICHWORT = 40;
const MAX_RUECKFRAGE = 500; // bruecke.py MAX_FRAGE

const $ = (id) => document.getElementById(id);
const katex = () => window.katex;

const z = {
  kurse: [],
  kurs: null, // {course_id, titel}
  turn: null,
  nummer: null,
  phase: "aufgabe", // aufgabe | rueckmeldung | ende
  rueckmeldung: null, // {state, text}
  tutor: [], // [{frage, teile}] Gespraech mit dem Tutor zur aktuellen Aufgabe
  busy: false,
  ergebnis: null, // Vorbewertung der offenen Flächenaufgabe: {vorbewertung_id, urteil, lesbar}
  korrektur: null, // null | "falsch" | "eintippen": Eingabefeld der Leiste offen
  hinweis: null, // ruhiger Hinweis in der Leiste (gehört zur Aufgabe)
  speicherHinweis: null, // „Nicht gespeichert“: bleibt über „Weiter“ stehen, bis ein Speichern klappt
  speichert: null, // laufendes Speichern (Promise); es läuft nie mehr als eines
  token: 0, // zählt Kurswechsel: Ergebnisse eines früheren Kurses werden verworfen
};

// ---- Hilfen -------------------------------------------------------------------------------
function el(tag, eigenschaften = {}, ...kinder) {
  const knoten = document.createElement(tag);
  for (const [k, v] of Object.entries(eigenschaften)) {
    if (k === "class") knoten.className = v;
    else if (k === "text") knoten.textContent = v;
    else if (k.startsWith("on")) knoten.addEventListener(k.slice(2), v);
    else if (v === true) knoten.setAttribute(k, "");
    else if (v !== false && v != null) knoten.setAttribute(k, String(v));
  }
  for (const kind of kinder) if (kind != null) knoten.append(kind);
  return knoten;
}

function setzeText(knoten, text, mathe) {
  if (!mathe) {
    knoten.textContent = String(text ?? "");
    return knoten;
  }
  const r = formel(text, katex());
  if (r.html !== undefined) knoten.innerHTML = r.html; // KaTeX-Ausgabe, Rest maskiert
  else knoten.textContent = r.text;
  return knoten;
}

function speicher(op, schluessel, wert) {
  try {
    return op === "lesen" ? window.localStorage.getItem(schluessel) : window.localStorage.setItem(schluessel, wert);
  } catch {
    return null;
  }
}

function banner(text) {
  const b = $("banner");
  b.textContent = text ?? "";
  b.hidden = !text;
}

async function api(methode, pfad, body) {
  try {
    const antwort = await fetch(API + pfad, {
      method: methode,
      credentials: "same-origin",
      headers: body === undefined ? {} : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const daten = await antwort.json().catch(() => ({}));
    return { status: antwort.status, ok: antwort.ok, daten };
  } catch {
    return { status: 0, ok: false, daten: {} };
  }
}

const mathe = () => z.turn?.task?.presentation?.math === true;

// ---- Layout ---------------------------------------------------------------------------------
function layoutSetzen(art) {
  const wahl = art === "c" ? "c" : "a";
  document.body.classList.toggle("layout-a", wahl === "a");
  document.body.classList.toggle("layout-c", wahl === "c");
  $("layout-a").setAttribute("aria-pressed", String(wahl === "a"));
  $("layout-c").setAttribute("aria-pressed", String(wahl === "c"));
  speicher("schreiben", "huelle-layout", wahl);
}

// ---- Kopf ----------------------------------------------------------------------------------
function fortschrittZeigen() {
  const t = z.turn;
  const knoten = $("fortschritt");
  if (!t || !t.task || z.phase === "ende") {
    knoten.textContent = "";
    return;
  }
  const nummer = z.nummer ?? (t.progress?.completed ?? 0) + 1;
  knoten.textContent = `Aufgabe ${nummer} von ${t.progress?.total ?? "?"}`;
}

// ---- Karte -----------------------------------------------------------------------------------
function hilfeLeeren() {
  z.tutor = [];
  $("hilfe").replaceChildren();
}

function hilfeZeigen(inhalt) {
  const box = el("div", { class: "antwortbox" });
  for (const absatz of inhalt) box.append(setzeText(el("p"), absatz, mathe()));
  $("hilfe").replaceChildren(box);
}

function karteAufgabe() {
  const t = z.turn;
  const task = t.task;
  const math = task.presentation?.math === true;
  z.aufgabeMathe = math;
  const sperrText = sperrHinweis(t);
  const hilfeKnoepfe = [
    el("button", { type: "button", id: "erklaeren", text: "Erklär mir das", onclick: () => erklaeren() }),
    el("button", { type: "button", id: "hinweis", text: "Hinweis", onclick: hinweis }),
  ];
  const sperre = sperrText ? el("p", { class: "hinweistext sperrhinweis", text: sperrText }) : null;
  if (task.type !== "code") {
    // Alles andere wird auf der Fläche beantwortet; Abgeben, Passt usw. sitzen in der Leiste über der Fläche.
    $("karte").replaceChildren(
      setzeText(el("p", { class: "aufgabe" }), task.prompt, math),
      el("p", { class: "hinweistext", text: TEXT.aufFlaeche }),
      sperre,
      el("div", { class: "knoepfe" }, ...hilfeKnoepfe));
    return;
  }
  const feld = el("textarea", { name: "antwort", required: true, placeholder: "Lösung und Begründung eingeben" });
  const form = el("form", { novalidate: false },
    setzeText(el("p", { class: "aufgabe" }), task.prompt, math),
    el("div", {}, feld, el("p", { class: "hinweistext", text: TEXT.rechnen })),
    sperre,
    el("div", { class: "knoepfe" },
      el("button", { type: "submit", class: "haupt-knopf", id: "abgeben", text: "Abgeben" }),
      ...hilfeKnoepfe));
  form.addEventListener("submit", (ereignis) => {
    ereignis.preventDefault();
    const antwort = antwortAus(task, { text: feld.value });
    if (antwort) void antwortSenden(antwort);
  });
  $("karte").replaceChildren(form);
}

function absaetze(text) {
  return String(text).split(/\n{2,}/).map((s) => s.trim()).filter(Boolean);
}

function karteRueckmeldung() {
  const r = z.rueckmeldung ?? { state: null, text: "" };
  const art = r.state === "correct" ? "richtig" : r.state === "needs_review" ? "pruefung" : "falsch";
  const box = el("div", { class: `rueckmeldung ${art}`, role: "status" });
  const text = r.state === "needs_review" && !r.text ? TEXT.wirdGeprueft : r.text;
  for (const absatz of absaetze(text)) box.append(setzeText(el("p"), absatz, z.aufgabeMathe === true));
  $("karte").replaceChildren(box, el("div", { class: "knoepfe" },
    el("button", { type: "button", class: "haupt-knopf", id: "weiter", text: "Weiter", onclick: () => weiter() })));
}

function karteLeer() {
  $("karte").replaceChildren();
}

// ---- Bewertung am Ende -----------------------------------------------------------------------
async function karteBewertung() {
  const kurs = z.kurs;
  let smiley = null;
  let beruehrt = false;
  let gespeichert = false;
  const woerter = [0, 1, 2].map(() => el("input", { type: "text", maxlength: MAX_STICHWORT }));
  const status = el("div", { role: "status" });
  const fehler = el("p", { class: "fehler", role: "alert", hidden: true });
  const senden = el("button", { type: "button", class: "haupt-knopf", text: "Absenden", disabled: true });
  const super_ = el("button", { type: "button", text: "Alles super" });
  const tasten = SMILEYS.map((s) => el("button", {
    type: "button", "aria-label": s.wert, "aria-pressed": "false",
    onclick: () => { beruehrt = true; smiley = s.wert; gespeichert = false; aktualisieren(); },
  }, el("span", { "aria-hidden": "true", text: s.zeichen })));

  function aktualisieren() {
    tasten.forEach((t, i) => t.setAttribute("aria-pressed", String(SMILEYS[i].wert === smiley)));
    senden.disabled = !smiley || z.busy;
    status.replaceChildren(...(gespeichert
      ? [el("p", { class: "danke", text: "Danke für Ihre Bewertung!" }), el("p", { text: "Sie können sie jederzeit ändern." })]
      : []));
  }

  async function absenden(wahl, stichworte) {
    const sauber = stichworte.map((s) => s.trim()).filter(Boolean);
    if (sauber.some((s) => s.length > MAX_STICHWORT)) {
      fehler.textContent = `Ein Stichwort darf höchstens ${MAX_STICHWORT} Zeichen lang sein.`;
      fehler.hidden = false;
      return;
    }
    fehler.hidden = true;
    z.busy = true; aktualisieren();
    const r = await api("POST", `/kurse/${kurs.course_id}/bewertung`, { smiley: wahl, stichworte: sauber.slice(0, 3) });
    z.busy = false;
    if (r.ok) gespeichert = true;
    else { fehler.textContent = TEXT.bewertungFehler; fehler.hidden = false; }
    aktualisieren();
  }

  senden.addEventListener("click", () => smiley && void absenden(smiley, woerter.map((w) => w.value)));
  super_.addEventListener("click", () => {
    beruehrt = true; smiley = "gut"; woerter.forEach((w) => { w.value = ""; });
    void absenden("gut", []);
  });
  for (const w of woerter) w.addEventListener("input", () => { beruehrt = true; gespeichert = false; aktualisieren(); });

  const nochmal = el("button", {
    type: "button", text: "Noch einmal üben",
    onclick: () => { if (!z.busy) void kursOeffnen(kurs, "neu"); },
  });
  $("karte").replaceChildren(el("section", { class: "abschluss" },
    el("h2", { text: "Kurs abgeschlossen" }),
    el("p", { text: "Sie haben alle Aufgaben dieses Durchlaufs bearbeitet. Ihre Begriffe sehen Sie unten." }),
    el("div", { class: "knoepfe" }, nochmal)), el("section", { class: "bewertung" },
    el("h2", { text: "Wie fanden Sie diesen Kurs?" }),
    el("div", { class: "smileys" }, ...tasten),
    el("p", { text: "Was würden Sie besser machen? Drei kurze Stichworte – oder keins, wenn alles gepasst hat." }),
    ...woerter,
    el("div", { class: "knoepfe" }, senden, super_),
    fehler, status));
  aktualisieren();

  // Eigene fruehere Bewertung vorbelegen, solange nichts angefasst wurde.
  const r = await api("GET", `/kurse/${kurs.course_id}/bewertung`);
  const eigene = r.ok ? r.daten?.bewertung : null;
  if (eigene && eigene.smiley && !beruehrt && z.kurs === kurs) {
    smiley = eigene.smiley;
    (eigene.stichworte ?? []).slice(0, 3).forEach((s, i) => { woerter[i].value = s; });
    gespeichert = true;
    aktualisieren();
  }
}

// ---- Seitenbereiche ----------------------------------------------------------------------------
function begriffeZeigen(begriffe) {
  const liste = $("begriffe");
  liste.replaceChildren(...begriffeZusammenfassen(begriffe).map((b) => {
    const wert = typeof b.beherrschung === "number" ? Math.max(0, Math.min(1, b.beherrschung)) : null;
    const fuellung = el("span");
    fuellung.style.width = wert === null ? "0%" : `${Math.round(wert * 100)}%`;
    return el("li", {},
      el("div", { class: "begriff-titel" },
        el("span", { text: klartext(begriffTitel(b.titel)) }),
        el("span", { text: wert === null ? "—" : `${Math.round(wert * 100)} %` })),
      el("div", { class: "balken", role: "img", "aria-label": wert === null ? "—" : `${Math.round(wert * 100)} %` }, fuellung));
  }));
  $("begriffe-box").hidden = liste.children.length === 0;
}

function flaechenHinweisZeigen(daten) {
  $("flaechen-hinweis-text").textContent = FLAECHEN_HINWEIS;
  $("flaechen-hinweis").hidden = !flaechenHinweis(daten);
}

// Erneutes Oeffnen des aktuellen Kurses nur zum Pruefen der Verbindung: nichts wird abgegeben oder neu geladen.
async function flaecheVerbinden() {
  const kurs = z.kurs;
  if (!kurs) return;
  const r = await api("POST", `/kurse/${kurs.course_id}/oeffnen`);
  if (z.kurs !== kurs) return;
  if (!r.ok) { fehlerBehandeln(r); return; }
  flaechenHinweisZeigen(r.daten);
}

async function notizenLaden() {
  const kurs = z.kurs;
  if (!kurs) return;
  const r = await api("GET", `/notizen?course_id=${encodeURIComponent(kurs.course_id)}`);
  if (z.kurs !== kurs) return;
  const notizen = r.ok && Array.isArray(r.daten.notes) ? r.daten.notes : [];
  $("notizen").replaceChildren(...notizen.map((n) => el("li", {},
    el("p", { class: "auswahl", text: klartext(n.selection ?? n.title ?? "") }),
    setzeText(el("p", { class: "erklaerung" }), n.explanation ?? "", true))));
  $("notizen-box").hidden = notizen.length === 0;
}

// ---- Zustand zeigen ------------------------------------------------------------------------------
function zeigen() {
  const t = z.turn;
  fortschrittZeigen();
  hilfeLeeren();
  leisteZeigen();
  if (!t) { karteLeer(); return; }
  if (z.phase === "rueckmeldung") { karteRueckmeldung(); return; }
  if (t.task && t.state === "active") { z.phase = "aufgabe"; karteAufgabe(); return; }
  z.phase = "ende";
  fortschrittZeigen();
  void karteBewertung();
}

function turnUebernehmen(turn, nummer) {
  z.turn = turn;
  z.nummer = nummer ?? null;
}

function fehlerBehandeln(r) {
  banner(meldungFuer(r.daten));
  // Bei 409 mit Turn: den aktuellen Stand zeigen.
  if (r.status === 409 && r.daten?.turn) {
    z.rueckmeldung = null;
    z.phase = "aufgabe";
    neueAufgabe();
    const abgeschlossen = r.daten.turn.progress?.completed;
    turnUebernehmen(r.daten.turn, typeof abgeschlossen === "number" ? abgeschlossen + 1 : null);
    // Flaeche und Sperre fuer eine neue Aufgabe setzt nur `weiter`.
    if (nach409(r.daten.turn) === "weiter") void weiter(true);
    else zeigen();
  }
}

// ---- Aktionen ---------------------------------------------------------------------------------------
function sperren(an) {
  z.busy = an;
  for (const b of $("karte").querySelectorAll("button")) b.disabled = an;
  leisteZeigen();
}

// Eine andere Aufgabe (oder ein neuer Kurs): Ergebnis, Eingabefeld und Hinweis der Leiste gehören zur alten.
function neueAufgabe(hinweisBehalten = false) {
  z.ergebnis = null;
  z.korrektur = null;
  if (!hinweisBehalten) z.hinweis = null;
}

// `aktion` "neu" startet nach dem Kursende bewusst einen frischen Durchlauf.
async function kursOeffnen(kurs, aktion = "oeffnen") {
  banner("");
  $("flaechen-hinweis").hidden = true;
  z.kurs = kurs;
  z.token += 1;
  z.speicherHinweis = null;
  z.turn = null;
  z.phase = "aufgabe";
  z.rueckmeldung = null;
  neueAufgabe();
  zeigen();
  begriffeZeigen([]);
  $("notizen-box").hidden = true;
  const r = await api("POST", `/kurse/${kurs.course_id}/${aktion === "neu" ? "neu" : "oeffnen"}`);
  if (z.kurs !== kurs) return;
  if (!r.ok) { fehlerBehandeln(r); return; }
  begriffeZeigen(r.daten.begriffe);
  turnUebernehmen(r.daten.turn, r.daten.nummer);
  flaechenHinweisZeigen(r.daten);
  if (r.daten.flaeche === false && !flaechenHinweis(r.daten)) banner(TEXT.flaecheWeg);
  zeigen();
  void notizenLaden();
}

async function antwortSenden(antwort) {
  const t = z.turn;
  sperren(true);
  banner("");
  const r = await api("POST", `/sitzungen/${t.session_id}/antwort`, { revision: t.session_revision, answer: antwort });
  if (!r.ok) { sperren(false); fehlerBehandeln(r); return; }
  rueckmeldungZeigen(r.daten);
}

function rueckmeldungZeigen(daten) {
  const turn = daten.turn;
  z.turn = turn;
  z.nummer = daten.nummer ?? null;
  z.busy = false;
  if (Array.isArray(daten.begriffe)) begriffeZeigen(daten.begriffe);
  const state = turn?.feedback?.state ?? (daten.meldung ? "needs_review" : null);
  const text = typeof daten.rueckmeldung === "string" ? daten.rueckmeldung : "";
  if (!state && !text) { void weiter(); return; }
  z.rueckmeldung = { state, text };
  z.phase = "rueckmeldung";
  zeigen();
  void notizenLaden();
}

async function weiter(bannerBehalten = false) {
  const t = z.turn;
  sperren(true);
  if (bannerBehalten !== true) banner("");
  const r = await api("POST", `/sitzungen/${t.session_id}/weiter`);
  z.busy = false;
  if (!r.ok) { sperren(false); fehlerBehandeln(r); return; }
  z.rueckmeldung = null;
  z.phase = "aufgabe";
  neueAufgabe(bannerBehalten === true);
  turnUebernehmen(r.daten.turn, r.daten.nummer);
  zeigen();
  void speichern(false);
}

// "Erklär mir das" beginnt ein Gespraech, Rueckfragen fuehren es fort (die Bruecke gibt den Verlauf mit).
async function erklaeren(frage) {
  const t = z.turn;
  const rueckfrage = typeof frage === "string" && frage.trim() !== "";
  if (!rueckfrage) z.tutor = [];
  banner("");
  tutorZeigen(true);
  const r = await api("POST", `/sitzungen/${t.session_id}/erklaeren`, rueckfrage ? { frage: frage.trim() } : {});
  if (z.turn !== t) return;
  if (!r.ok) { tutorZeigen(false); fehlerBehandeln(r); return; }
  const teile = [r.daten.answer].filter((s) => typeof s === "string" && s);
  z.tutor.push({ frage: rueckfrage ? frage.trim() : null, teile });
  tutorZeigen(false);
}

function tutorZeigen(wartet) {
  const box = el("div", { class: "antwortbox tutor" });
  for (const eintrag of z.tutor) {
    if (eintrag.frage) box.append(el("p", { class: "eigene-frage", text: eintrag.frage }));
    for (const absatz of eintrag.teile) box.append(setzeText(el("p"), absatz, mathe()));
  }
  const feld = el("textarea", {
    rows: "3", maxlength: String(MAX_RUECKFRAGE), autocomplete: "off",
    "aria-label": "Rückfrage", placeholder: "Ihre Rückfrage oder Ihre Zwischenschritte",
  });
  const fragen = el("button", { type: "submit", text: wartet ? "Einen Moment …" : "Fragen", disabled: wartet });
  const form = el("form", { class: "rueckfrage" }, feld, el("div", { class: "knoepfe" }, fragen));
  form.addEventListener("submit", (ereignis) => {
    ereignis.preventDefault();
    if (feld.value.trim()) void erklaeren(feld.value);
  });
  if (z.tutor.length > 0 || wartet) box.append(form);
  $("hilfe").replaceChildren(box);
}

async function hinweis() {
  const t = z.turn;
  banner("");
  const r = await api("POST", `/sitzungen/${t.session_id}/hinweis`);
  if (!r.ok) { fehlerBehandeln(r); return; }
  if (typeof r.daten.hinweis === "string" && r.daten.hinweis) { z.tutor = []; hilfeZeigen([r.daten.hinweis]); }
}

// ---- Leiste über der Fläche: Speichern, Abgeben, Passt, Überarbeiten, Falsch gelesen ---------------------
const pause = (ms) => new Promise((fertig) => setTimeout(fertig, ms));

/**
 * Speichern über PenEchos eigenen Knopf (R7). Läuft schon eines, wird dessen Ergebnis geteilt statt ein
 * zweites zu starten. Bei Erfolg bleibt es still; sonst steht ein ruhiger Dauerhinweis in der Leiste.
 */
function speichern(manuell) {
  if (z.speichert) return z.speichert;
  const token = z.token;
  const lauf = (async () => {
    const r = await speichernMitWiederholung(() => flaecheSpeichern($("rahmen")));
    z.speichert = null;
    const aus = speicherAusgang(r, { manuell, token, aktuell: z.token, alt: z.speicherHinweis });
    z.speicherHinweis = aus.speicher;
    if (aus.meldung) {
      z.hinweis = aus.meldung;
      setTimeout(() => { if (z.hinweis === aus.meldung) { z.hinweis = null; leisteZeigen(); } }, 3000);
    }
    leisteZeigen();
    return r;
  })();
  z.speichert = lauf;
  leisteZeigen();
  return lauf;
}

function leisteZeigen() {
  const leiste = $("flaechenleiste");
  const t = z.turn;
  const zustand = z.phase === "aufgabe" && t?.state === "active"
    ? leistenZustand(t.task, z.ergebnis)
    : { sichtbar: false, knoepfe: [] };
  leiste.hidden = !zustand.sichtbar;
  if (!zustand.sichtbar) return;
  const aktion = {
    speichern: () => { if (!z.speichert) void speichern(true); },
    abgeben: () => void abgeben(),
    passt: () => void passt(),
    ueberarbeiten: () => ueberarbeiten(),
    falsch: () => korrekturOeffnen("falsch"),
    eintippen: () => korrekturOeffnen("eintippen"),
  };
  $("leisten-knoepfe").replaceChildren(...zustand.knoepfe.map((id) => el("button", {
    type: "button", id: `leiste-${id}`, class: id === "abgeben" ? "haupt-knopf" : "",
    disabled: z.busy || (id === "speichern" && z.speichert !== null), text: KNOPF_TEXT[id], onclick: aktion[id],
  })));
  const text = z.busy ? TEXT.wirdBewertet : (z.hinweis ?? z.speicherHinweis ?? ergebnisHinweis(z.ergebnis));
  $("leisten-hinweis").textContent = text ?? "";
  $("leisten-hinweis").hidden = !text;
  const offen = z.korrektur !== null && z.ergebnis !== null && zustand.knoepfe.includes(z.korrektur === "falsch" ? "falsch" : "eintippen");
  $("korrektur").hidden = !offen;
  if (offen) $("korrektur-text").textContent = eingabeHinweis(t.task.type) ?? "";
  $("neu-bewerten").disabled = z.busy;
}

function korrekturOeffnen(art) {
  z.korrektur = art;
  z.hinweis = null;
  $("korrektur-feld").value = "";
  leisteZeigen();
  $("korrektur-feld").focus();
}

function ueberarbeiten() {
  z.ergebnis = null;
  z.korrektur = null;
  z.hinweis = TEXT.ueberarbeiten;
  leisteZeigen();
}

// Eine Fehlerantwort der Leiste: ruhiger Hinweis; bei veralteter Karte der aktuelle Stand aus `turn`.
function leistenFehler(r) {
  const f = fehlerText(r);
  if (f.veraltet) {
    neueAufgabe(true);
    z.rueckmeldung = null;
    z.phase = "aufgabe";
    const abgeschlossen = r.daten.turn.progress?.completed;
    turnUebernehmen(r.daten.turn, typeof abgeschlossen === "number" ? abgeschlossen + 1 : null);
    z.hinweis = f.text;
    if (nach409(r.daten.turn) === "weiter") void weiter(true);
    else zeigen();
    return;
  }
  z.hinweis = f.text;
  leisteZeigen();
}

// Antwort von abgeben/korrektur: Vorbewertung merken, Knöpfe nach Urteil, danach speichern.
function ergebnisUebernehmen(r) {
  sperren(false);
  if (!r.ok) { leistenFehler(r); return; }
  if (r.daten.turn) z.turn = r.daten.turn;
  z.ergebnis = ergebnisAus(r.daten);
  z.korrektur = null;
  z.hinweis = null;
  leisteZeigen();
  void speichern(false);
}

async function abgeben() {
  const t = z.turn;
  if (!t || z.busy) return;
  z.hinweis = null;
  banner("");
  sperren(true);
  ergebnisUebernehmen(await api("POST", `/sitzungen/${t.session_id}/abgeben`, { revision: t.session_revision }));
}

async function korrekturSenden() {
  const t = z.turn;
  const ergebnis = z.ergebnis;
  if (!t || !ergebnis || z.busy) return;
  const antwort = korrekturAntwort(t.task, $("korrektur-feld").value);
  if (!antwort) {
    z.hinweis = `${TEXT.nichtZugeordnet} ${eingabeHinweis(t.task.type) ?? ""}`.trim();
    leisteZeigen();
    return;
  }
  z.hinweis = null;
  sperren(true);
  ergebnisUebernehmen(await api("POST", `/sitzungen/${t.session_id}/korrektur`,
    { revision: t.session_revision, vorbewertung_id: ergebnis.vorbewertung_id, answer: antwort }));
}

async function passt() {
  const t = z.turn;
  const ergebnis = z.ergebnis;
  if (!t || !ergebnis || z.busy) return;
  z.hinweis = null;
  banner("");
  sperren(true);
  const r = await api("POST", `/sitzungen/${t.session_id}/passt`, { revision: t.session_revision, vorbewertung_id: ergebnis.vorbewertung_id });
  if (!r.ok) { sperren(false); leistenFehler(r); return; }
  neueAufgabe();
  turnUebernehmen(r.daten.turn, r.daten.nummer ?? null);
  if (Array.isArray(r.daten.begriffe)) begriffeZeigen(r.daten.begriffe);
  // Erst die fertige Aufgabe sichern, bevor `weiter` das Board umstellt; höchstens kurz warten.
  await Promise.race([speichern(false), pause(BEWERTET_NACH_MS)]);
  await weiter();
  void notizenLaden();
}

// ---- Start --------------------------------------------------------------------------------------------
async function start() {
  layoutSetzen(speicher("lesen", "huelle-layout"));
  $("flaeche-verbinden").addEventListener("click", () => { void flaecheVerbinden(); });
  $("korrektur").addEventListener("submit", (ereignis) => { ereignis.preventDefault(); void korrekturSenden(); });
  $("layout-a").addEventListener("click", () => layoutSetzen("a"));
  $("layout-c").addEventListener("click", () => layoutSetzen("c"));
  const r = await api("GET", "/kurse");
  if (!r.ok || !Array.isArray(r.daten) || r.daten.length === 0) {
    banner(r.ok ? TEXT.keinKurs : meldungFuer(r.daten));
    return;
  }
  z.kurse = r.daten;
  const wahl = $("kurs");
  wahl.replaceChildren(...z.kurse.map((k) => el("option", { value: k.course_id, text: k.titel })));
  const gemerkt = speicher("lesen", "huelle-kurs");
  if (gemerkt && z.kurse.some((k) => k.course_id === gemerkt)) wahl.value = gemerkt;
  wahl.addEventListener("change", () => {
    speicher("schreiben", "huelle-kurs", wahl.value);
    void kursOeffnen(z.kurse.find((k) => k.course_id === wahl.value));
  });
  await kursOeffnen(z.kurse.find((k) => k.course_id === wahl.value) ?? z.kurse[0]);
}

start();
