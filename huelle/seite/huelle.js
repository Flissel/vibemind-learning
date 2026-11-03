// Bedienteil der Lern-Huelle: Aufgabenkarte neben der PenEcho-Flaeche.
import {
  antwortAus, bildAus, fensterAus, formel, klartext, meldungFuer, nach409, prozent, sperrHinweis, SPERR_HINWEIS,
} from "./aufgabe.js";

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
  zahl: "Bitte eine Zahl eingeben, z. B. -1,5",
  rechnen: "Sie können auf der Fläche rechnen oder zeichnen.",
};
const SHELLS = { powershell: "Windows PowerShell", cmd: "Windows CMD", unix: "macOS / Linux" };
const SMILEYS = [
  { wert: "schlecht", zeichen: "😕" },
  { wert: "mittel", zeichen: "🙂" },
  { wert: "gut", zeichen: "😃" },
];
const MAX_STICHWORT = 40;

const $ = (id) => document.getElementById(id);
const katex = () => window.katex;

const z = {
  kurse: [],
  kurs: null, // {course_id, titel}
  turn: null,
  nummer: null,
  phase: "aufgabe", // aufgabe | rueckmeldung | ende
  rueckmeldung: null, // {state, text}
  busy: false,
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
  $("hilfe").replaceChildren();
}

function hilfeZeigen(inhalt) {
  const box = el("div", { class: "antwortbox" });
  for (const absatz of inhalt) box.append(setzeText(el("p"), absatz, mathe()));
  $("hilfe").replaceChildren(box);
}

function option(typ, name, wert, label, math) {
  const eingabe = el("input", { type: typ, name, value: wert });
  return el("label", { class: "option" }, eingabe, setzeText(el("span"), label, math));
}

function reihenfolgeFeld(items, math) {
  let ordnung = items.map((i) => i.id);
  const label = Object.fromEntries(items.map((i) => [i.id, i.label]));
  const liste = el("div", { role: "list", "aria-label": "Reihenfolge" });
  const zeichnen = () => {
    liste.replaceChildren(...ordnung.map((id, index) => {
      const bewege = (d) => {
        const ziel = index + d;
        if (ziel < 0 || ziel >= ordnung.length) return;
        [ordnung[index], ordnung[ziel]] = [ordnung[ziel], ordnung[index]];
        zeichnen();
      };
      return el("div", { class: "reihe", role: "listitem" },
        el("span", { text: `${index + 1}.` }),
        setzeText(el("span", { class: "titel" }), label[id], math),
        el("button", { type: "button", "aria-label": "Nach oben", disabled: index === 0, text: "↑", onclick: () => bewege(-1) }),
        el("button", { type: "button", "aria-label": "Nach unten", disabled: index === ordnung.length - 1, text: "↓", onclick: () => bewege(1) }),
      );
    }));
  };
  zeichnen();
  return { knoten: liste, lesen: () => ({ reihenfolge: [...ordnung] }) };
}

function bildKnoten(bild, alt) {
  return el("img", { class: "bild", src: bild.src, alt, width: bild.width, height: bild.height, loading: "lazy" });
}

/** Lueckentext im gezeichneten Fenster (wie TaskRenderer.tsx); laedt das Bild nicht, kommt die Textzeile. */
function fensterFeld(task, fenster) {
  const shell = typeof task.presentation?.shell === "string" ? task.presentation.shell : undefined;
  const name = shell ? SHELLS[shell] ?? shell : "Terminal";
  const eingabe = el("input", {
    type: "text", name: "antwort", class: "fenster-luecke", "aria-label": `Lücke im Fenster von ${name}`,
    autocomplete: "off", spellcheck: "false", required: true,
  });
  Object.assign(eingabe.style, prozent(fenster.rect, fenster));
  const spiegel = fenster.more.map((r) => {
    const s = el("span", { class: "fenster-spiegel", "aria-hidden": "true" });
    Object.assign(s.style, prozent(r, fenster));
    return s;
  });
  eingabe.addEventListener("input", () => { for (const s of spiegel) s.textContent = eingabe.value; });
  const bild = el("img", { src: fenster.src, alt: `Fenster von ${name}`, width: fenster.width, height: fenster.height });
  const rahmen = el("div", { class: "fenster" }, bild, eingabe, ...spiegel);
  rahmen.style.aspectRatio = `${fenster.width} / ${fenster.height}`;
  const knoten = el("div", {}, rahmen);
  const feld = { knoten, lesen: () => ({ text: eingabe.value }) };
  bild.addEventListener("error", () => {
    const ersatz = textLuecke(task);
    knoten.replaceChildren(ersatz.knoten);
    feld.lesen = ersatz.lesen;
  });
  return feld;
}

function luckenFeld(task) {
  const fenster = fensterAus(task.presentation);
  return fenster ? fensterFeld(task, fenster) : textLuecke(task);
}

function textLuecke(task) {
  const vorlage = typeof task.presentation?.template === "string" ? task.presentation.template : "{{wert}}";
  const shell = typeof task.presentation?.shell === "string" ? task.presentation.shell : undefined;
  const teile = vorlage.split(/(\{\{\w+\}\})/).filter(Boolean);
  const eingabe = el("input", { type: "text", name: "antwort", "aria-label": "Lücke", autocomplete: "off", spellcheck: "false", required: true });
  let gesetzt = false;
  const pre = el("div", { class: "luecke" });
  for (const teil of teile) {
    if (/^\{\{\w+\}\}$/.test(teil)) {
      if (gesetzt) pre.append(el("span", { text: "…" }));
      else { gesetzt = true; pre.append(eingabe); }
    } else {
      pre.append(setzeText(el("span"), teil, task.presentation?.math === true));
    }
  }
  const knoten = el("div", {}, shell ? el("p", { class: "hinweistext", text: `Für ${SHELLS[shell] ?? shell}` }) : null, pre);
  return { knoten, lesen: () => ({ text: eingabe.value }) };
}

function zuordnungFeld(task, math) {
  const links = Array.isArray(task.presentation?.left) ? task.presentation.left : [];
  const rechts = Array.isArray(task.presentation?.right) ? task.presentation.right : [];
  const knoten = el("div");
  // Die rechte Seite als sichtbare Liste (Bilder wie „Graph 1“, gesetzte Formeln); ein <option> zeigt nur Text.
  if (math || rechts.some((r) => bildAus(r))) {
    knoten.append(el("ul", { class: "auswahlliste", "aria-label": "Zur Auswahl" }, ...rechts.map((r) => {
      const bild = bildAus(r);
      return el("li", {}, setzeText(el("span", { class: "titel" }), r.label, math), bild ? bildKnoten(bild, r.label) : null);
    })));
  }
  const selects = new Map();
  for (const eintrag of links) {
    const auswahl = el("select", { name: `paar-${eintrag.id}`, required: true },
      el("option", { value: "", disabled: true, selected: true, text: "– bitte wählen –" }),
      ...rechts.map((r) => el("option", { value: r.id, text: math ? klartext(r.label) : r.label })));
    selects.set(eintrag.id, auswahl);
    const bild = bildAus(eintrag);
    knoten.append(el("label", { class: "zeile" }, setzeText(el("span", { class: "titel" }), eintrag.label, math),
      bild ? bildKnoten(bild, eintrag.label) : null, auswahl));
  }
  return { knoten, lesen: () => ({ paare: Object.fromEntries([...selects].map(([id, s]) => [id, s.value])) }) };
}

/** Das Eingabefeld je Typ; `lesen()` liefert die Eingabe fuer `antwortAus`. */
function feldFuer(task) {
  const math = task.presentation?.math === true;
  switch (task.type) {
    case "single_choice":
    case "multiple_choice": {
      const mehrere = task.type === "multiple_choice";
      const satz = el("fieldset", {}, el("legend", { class: "nur-leser", text: "Antwort auswählen" }),
        ...(task.options ?? []).map((o) => option(mehrere ? "checkbox" : "radio", "antwort", o.id, o.label, math)));
      if (!mehrere) for (const r of satz.querySelectorAll("input")) r.required = true;
      return { knoten: satz, lesen: () => ({ auswahl: [...satz.querySelectorAll("input:checked")].map((i) => i.value) }) };
    }
    case "ordering":
      return reihenfolgeFeld(task.options ?? [], math);
    case "matching":
      return zuordnungFeld(task, math);
    case "numeric": {
      const einheit = typeof task.presentation?.unit === "string" ? task.presentation.unit : "";
      const eingabe = el("input", { type: "text", name: "antwort", "aria-label": "Zahl eingeben", inputmode: "text", autocomplete: "off", spellcheck: "false", required: true });
      const fehler = el("p", { class: "fehler", role: "alert", hidden: true, text: TEXT.zahl });
      eingabe.addEventListener("input", () => { fehler.hidden = true; });
      const knoten = el("div", {}, el("label", { class: "zeile" }, einheit ? setzeText(el("span", { class: "titel" }), einheit, math) : null, eingabe), fehler);
      return { knoten, lesen: () => ({ text: eingabe.value }), ungueltig: () => { fehler.hidden = false; } };
    }
    case "fill_in":
      return luckenFeld(task);
    case "penecho_canvas":
      return { knoten: el("p", { class: "hinweistext", text: TEXT.rechnen }), lesen: () => ({}) };
    default: { // open_response, case, code
      const feld = el("textarea", { name: "antwort", required: true, placeholder: task.type === "code" ? "Lösung und Begründung eingeben" : "Antwort ausarbeiten" });
      const knoten = el("div", {}, feld, el("p", { class: "hinweistext", text: TEXT.rechnen }));
      return { knoten, lesen: () => ({ text: feld.value }) };
    }
  }
}

function karteAufgabe() {
  const t = z.turn;
  const task = t.task;
  const math = task.presentation?.math === true;
  z.aufgabeMathe = math;
  const feld = feldFuer(task);
  const zeichnung = task.type === "penecho_canvas";
  const abgeben = el("button", { type: "submit", class: "haupt-knopf", id: "abgeben", text: zeichnung ? "Zeichnung abgeben" : "Abgeben" });
  const sperrText = sperrHinweis(t);
  const form = el("form", { novalidate: false },
    setzeText(el("p", { class: "aufgabe" }), task.prompt, math),
    feld.knoten,
    sperrText ? el("p", { class: "hinweistext sperrhinweis", text: sperrText }) : null,
    el("div", { class: "knoepfe" },
      abgeben,
      el("button", { type: "button", id: "erklaeren", text: "Erklär mir das", onclick: erklaeren }),
      el("button", { type: "button", id: "hinweis", text: "Hinweis", onclick: hinweis })),
  );
  form.addEventListener("submit", (ereignis) => {
    ereignis.preventDefault();
    if (zeichnung) { void zeichnungAbgeben(); return; }
    const antwort = antwortAus(task, feld.lesen());
    if (!antwort) { feld.ungueltig?.(); return; }
    void antwortSenden(antwort);
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

  $("karte").replaceChildren(el("section", { class: "bewertung" },
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
  liste.replaceChildren(...(begriffe ?? []).map((b) => {
    const wert = typeof b.beherrschung === "number" ? Math.max(0, Math.min(1, b.beherrschung)) : null;
    const fuellung = el("span");
    fuellung.style.width = wert === null ? "0%" : `${Math.round(wert * 100)}%`;
    return el("li", {},
      el("div", { class: "begriff-titel" },
        el("span", { text: klartext(b.titel) }),
        el("span", { text: wert === null ? "—" : `${Math.round(wert * 100)} %` })),
      el("div", { class: "balken", role: "img", "aria-label": wert === null ? "—" : `${Math.round(wert * 100)} %` }, fuellung));
  }));
  $("begriffe-box").hidden = !begriffe || begriffe.length === 0;
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
}

async function kursOeffnen(kurs) {
  banner("");
  z.kurs = kurs;
  z.turn = null;
  z.phase = "aufgabe";
  z.rueckmeldung = null;
  zeigen();
  begriffeZeigen([]);
  $("notizen-box").hidden = true;
  const r = await api("POST", `/kurse/${kurs.course_id}/oeffnen`);
  if (z.kurs !== kurs) return;
  if (!r.ok) { fehlerBehandeln(r); return; }
  begriffeZeigen(r.daten.begriffe);
  turnUebernehmen(r.daten.turn, r.daten.nummer);
  if (r.daten.flaeche === false) banner(TEXT.flaecheWeg);
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

async function zeichnungAbgeben() {
  const t = z.turn;
  sperren(true);
  banner("");
  const r = await api("POST", `/sitzungen/${t.session_id}/zeichnung`, { revision: t.session_revision });
  if (!r.ok) { sperren(false); fehlerBehandeln(r); return; }
  rueckmeldungZeigen(r.daten);
}

function rueckmeldungZeigen(daten) {
  const turn = daten.turn;
  z.turn = turn;
  z.nummer = daten.nummer ?? null;
  z.busy = false;
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
  turnUebernehmen(r.daten.turn, r.daten.nummer);
  zeigen();
}

async function erklaeren() {
  const t = z.turn;
  banner("");
  const r = await api("POST", `/sitzungen/${t.session_id}/erklaeren`, {});
  if (!r.ok) { fehlerBehandeln(r); return; }
  const teile = [r.daten.answer, r.daten.next_step].filter((s) => typeof s === "string" && s);
  hilfeZeigen(teile);
}

async function hinweis() {
  const t = z.turn;
  banner("");
  const r = await api("POST", `/sitzungen/${t.session_id}/hinweis`);
  if (!r.ok) { fehlerBehandeln(r); return; }
  if (typeof r.daten.hinweis === "string" && r.daten.hinweis) hilfeZeigen([r.daten.hinweis]);
}

// ---- Start --------------------------------------------------------------------------------------------
async function start() {
  layoutSetzen(speicher("lesen", "huelle-layout"));
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
