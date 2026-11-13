// Bedienteil der Lern-Huelle: Aufgabenkarte neben der PenEcho-Flaeche.
import {
  abgabeNummerZulaessig, autoVerbindenFaellig, antwortAus, begriffeZusammenfassen, begriffTitel, eingabeHinweis, ergebnisAus, ergebnisHinweis, fehlerText,
  flaechenHinweis, FLAECHEN_HINWEIS, formel, klartext, korrekturAntwort, kaertchenAntwort, kaertchenKennung, kaertchenPaar, kaertchenStart, kaertchenTippen,
  KAERTCHEN_TYPEN, etiketten, kursVorwahl, leistenKnoepfe, leistenZustand,
  meldungFuer,
  nach409, NICHT_LESBAR, ohneLeere, speicherAusgang, speichernMitWiederholung, sperrHinweis, SPERR_HINWEIS,
} from "./aufgabe.js";
import {
  flaecheErneuern, flaechenAdresse, flaechenKennung, flaechenKopf, flaecheSpeichern, flaechenHerkunft, gruppeAusAdresse, kennungBelegt,
  mitFlaechenKennung, nachrichtAusFlaeche, neueFlaechenKennung, wiederholbar,
} from "./flaeche.js";
import {
  entwurfSperrt, entwurfText, eintragSetzen, FLAECHE_OHNE_AUFGABE, hinweisFuerAktiv, KURS_ABGEBEN_RUECKFRAGE,
  kursAbgabeText,
  kursAbgebenSichtbar, kursEndeLaden, LISTEN_MELDUNGEN, listenErgebnisAus, mussAbfragen, naechsteOffene, nummerAusDokument,
  nummerZurNachricht, standAnnehmen, statusText, warteText, zusammenfassungSessionId,
} from "./liste.js";

const API = "/lernen/api";
// Teil B: Flächen-Kennung dieses Tabs (sessionStorage); geht in die Adresse der Fläche und an jede API-Anfrage.
let kennung = flaechenKennung();
let erneuerung = null; // laufende Erneuerung der Kennung; gleichzeitige „belegt“-Antworten teilen sie
// Teil C, Gruppenfläche: `?gruppe=<id>` öffnet die eigene Mappe der Gruppe statt eines Kurses (sonst alles wie Solo).
const gruppeId = gruppeAusAdresse(location.search);
const GRUPPE_SCHLUESSEL = "lernflaeche-gruppe-"; // sessionStorage je Gruppe: {gruppe, person} für das Neuladen
const VERBINDEN_MS = 1500; // nach dem Laden der Gruppenfläche: so lange warten, bis sie sich beim MCP meldet

// Feste Texte (Plan, global.md).
const TEXT = {
  nichtErreichbar: "Learning ist gerade nicht erreichbar. Ihre Fläche bleibt erhalten.",
  schonAbgegeben: "Diese Aufgabe wurde schon abgegeben. Ich lade den aktuellen Stand.",
  keinKurs: "Dieser Kurs hat noch keine freigegebenen Aufgaben.",
  flaecheWeg: "Die Fläche ist gerade nicht erreichbar.",
  sperre: SPERR_HINWEIS,
  abgabeUnzulaessig: "Diese Aufgabe kann gerade nicht abgegeben werden.",
  nichtAngenommen: "Diese Abgabe wurde nicht angenommen. Bitte prüfen Sie Ihre Eingabe.",
  wirdGeprueft: "Ihre Abgabe wird noch geprüft. Sie zählt, sobald die Prüfung fertig ist.",
  keinHinweis: "Kein Hinweis verfügbar.",
  keinHinweisPruefung: "Im Prüfungsmodus gibt es keine Hinweise.",
  allgemein: "Das hat gerade nicht geklappt. Bitte versuchen Sie es noch einmal.",
  bewertungFehler: "Die Bewertung konnte nicht gespeichert werden. Bitte versuchen Sie es noch einmal.",
  rechnen: "Sie können auf der Fläche rechnen oder zeichnen.",
  aufFlaeche: "Lösen Sie die Aufgabe auf der Fläche. „Abgeben“ finden Sie oben rechts.",
  ueberarbeiten: "Ändern Sie Ihre Antwort auf der Fläche und drücken Sie dann erneut „Abgeben“.",
  nichtZugeordnet: "Das konnte ich nicht zuordnen.",
  wirdBewertet: "Wird bewertet …",
  kaertchenHinweis: "Antippen statt schreiben – oder weiter mit dem Stift auf der Fläche.",
  kaertchenAbgeben: "Mit Kärtchen abgeben",
  fallSeitenleiste: "Fall",
};
const BEWERTET_NACH_MS = 4000; // Passt wartet höchstens so lange auf das Speichern, dann geht es weiter
const ABFRAGE_MS = 2000; // Listenkurs: Stand abfragen, solange eine Abgabe bewertet wird
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
  // Listenkurs (nur wenn die Brücke `stand.ablauf === "liste"` meldet; sonst null und alles wie bisher):
  listeEnde: false, // beendeter Listenkurs, neu geöffnet: ohne Stand, aber der Link auf die Zusammenfassung gilt
  liste: null, // Stand der Brücke {ablauf, aktuell, wartend, aufgaben: [{nummer, status, urteil, vorbewertung_id, meldung}]}
  listenAufgaben: new Map(), // nummer -> task (vom Öffnen), für Leiste und „Falsch gelesen“
  gewaehlt: null, // gewählte Aufgabe; „Abgeben“, „Passt“ usw. gelten für sie
  verworfen: new Set(), // vorbewertung_id, deren Knöpfe „Überarbeiten“ ausgeblendet hat
  abfrage: null, // Zeitgeber der Standabfrage
  folge: 0, // laufende Nummer der Anfragen, die einen Stand bringen
  gueltigAb: 0, // Stände aus Anfragen davor gelten nicht (letzte Abgabe begonnen oder beantwortet)
  sendend: new Set(), // Nummern, deren Abgabe gerade unterwegs ist
  // Ein Canvas je Aufgabe (nur mit `je_aufgabe` der Brücke; sonst false/{}/null und alles wie bisher):
  jeAufgabe: false,
  dokumente: {}, // PenEcho-documentId -> Aufgabennummer
  nachladen: null, // laufendes Nachladen von `dokumente` (Promise); es läuft nie mehr als eines
  kursabgabe: null, // Stand von „Kurs abgeben“: {zustand: laeuft|fertig|fehler, aktuell, gesamt, meldung}
  kursAbgebenBusy: false, // der Aufruf „Kurs abgeben“ ist unterwegs
  // Gruppenfläche (Teil C): Gruppe aus der Adresse; Gruppe und Person der Fläche kommen aus der Antwort der Brücke.
  gruppeId,
  flaechenAbgabe: false, // eine Abgabe aus PenEcho läuft gerade an (Doppelklick: die zweite Nachricht wird verworfen)
  mcpBereit: false, // PenEcho hat „MCP bereit“ gemeldet (egal ob vor oder nach dem Öffnen)
  mcpAutoVerbunden: false, // „Fläche verbinden“ wurde auf PenEchos Meldung schon einmal ausgelöst (höchstens einmal je Laden)
  kaertchen: null, // {taskId, zustand}: Auswahl der Begriffs-Kärtchen der gezeigten Aufgabe
  rahmenGruppe: null, // {gruppe: "g-<id>", person} oder null
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

async function anfrage(methode, pfad, body) {
  try {
    const antwort = await fetch(API + pfad, {
      method: methode,
      credentials: "same-origin",
      headers: { ...flaechenKopf(kennung), ...(body === undefined ? {} : { "content-type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const daten = await antwort.json().catch(() => ({}));
    return { status: antwort.status, ok: antwort.ok, daten, belegt: kennungBelegt(antwort) };
  } catch {
    return { status: 0, ok: false, daten: {}, belegt: false };
  }
}

// Die Kennung dieses Tabs gehoert einer anderen Person (Personenwechsel im selben Tab): neue Kennung, die Flaeche
// laedt mit ihr neu. Kein Fehlertext; scheitert es erneut, zeigt die Antwort wie bisher „Flaeche verbinden“.
function kennungErneuern() {
  erneuerung ??= (async () => {
    try {
      // Gruppe ohne bekannte Person: die Fläche lädt erst mit Gruppe und Person (sonst arbeitete sie allein).
      if (z.gruppeId !== null && !z.rahmenGruppe) { kennung = neueFlaechenKennung(); return; }
      kennung = await flaecheErneuern({ rahmen: $("rahmen"), gruppe: z.rahmenGruppe });
    } finally {
      erneuerung = null;
    }
  })();
  return erneuerung;
}

function api(methode, pfad, body) {
  return mitFlaechenKennung(() => anfrage(methode, pfad, body), {
    erneuern: kennungErneuern,
    wiederholbar: wiederholbar(methode, pfad),
  });
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
  listenkopfZeigen();
  if (!t || !t.task || z.phase === "ende" || z.liste) {
    knoten.textContent = "";
    return;
  }
  const nummer = z.nummer ?? (t.progress?.completed ?? 0) + 1;
  knoten.textContent = `Aufgabe ${nummer} von ${t.progress?.total ?? "?"}`;
}

// ---- Listenkurs: Aufgabenwahl, Stand, Abfrage ---------------------------------------------------
const gewaehlteAufgabe = () => z.liste?.aufgaben.find((a) => a.nummer === z.gewaehlt) ?? null;

/** Ergebnis der gewählten Aufgabe für die Leiste: nur mit Rückmeldung, die nicht „überarbeitet“ wurde. */
function listenErgebnis(a) {
  return listenErgebnisAus(a, z.verworfen);
}

const listenMeldung = (daten) => (LISTEN_MELDUNGEN.has(daten?.meldung) ? daten.meldung : meldungFuer(daten));

/**
 * Stand der Brücke übernehmen, wenn er noch gilt (`standAnnehmen`: keine Antwort von vor der letzten
 * Abgabe, unterwegs befindliche Abgaben bleiben „in_bewertung“). Ohne gültige gewählte Aufgabe gilt
 * `aktuell`. Plant die Abfrage. `anfrage`: Nummer der Anfrage (`neueAnfrage()`), Infinity = eigene Antwort.
 */
function standAnwenden(stand, anfrage) {
  const neu = standAnnehmen(stand, { anfrage, gueltigAb: z.gueltigAb, sendend: z.sendend });
  if (!neu) return false;
  z.liste = neu;
  modellUebernehmen(stand);
  if (!neu.aufgaben.some((a) => a.nummer === z.gewaehlt)) z.gewaehlt = neu.aktuell;
  abfragePlanen();
  return true;
}

// `je_aufgabe`, `dokumente` und `kursabgabe` aus `/stand` und `/oeffnen`; fehlt ein Feld, bleibt der bisherige Wert.
function modellUebernehmen(stand) {
  dokumenteUebernehmen(stand);
  if (stand && "kursabgabe" in stand) z.kursabgabe = stand.kursabgabe ?? null;
}

// `je_aufgabe` und `dokumente` aus einer Antwort der Brücke (auch zeigen, weiter, passt, antwort, abgeben, korrektur:
// sie können ein Aufgaben-Canvas anlegen). Die Zuordnung wächst nur: ein Dokument gehört immer zu derselben Aufgabe,
// eine ältere Antwort nimmt also kein neueres Dokument weg.
function dokumenteUebernehmen(daten) {
  if (typeof daten?.je_aufgabe === "boolean") z.jeAufgabe = daten.je_aufgabe;
  if (daten?.dokumente && typeof daten.dokumente === "object") z.dokumente = { ...z.dokumente, ...daten.dokumente };
}

const neueAnfrage = () => { z.folge += 1; return z.folge; };

function abfrageStoppen() {
  if (z.abfrage !== null) clearTimeout(z.abfrage);
  z.abfrage = null;
}

// Alle 2 s, solange eine Aufgabe bewertet wird; sonst nicht.
function abfragePlanen() {
  if (z.abfrage !== null || !z.liste) return;
  if (!mussAbfragen(z.liste.aufgaben) && z.kursabgabe?.zustand !== "laeuft") return;
  const token = z.token;
  z.abfrage = setTimeout(async () => {
    z.abfrage = null;
    const t = z.turn;
    if (token !== z.token || !t || !z.liste) return;
    const nr = neueAnfrage();
    const r = await api("GET", `/sitzungen/${t.session_id}/stand`);
    if (token !== z.token || !z.liste) return;
    const vorher = z.kursabgabe?.zustand;
    if (r.ok && standAnwenden(r.daten, nr)) {
      listeZeigen();
      // „Kurs abgeben“ ist eben fertig: die Endansicht (Bewertung) kommt wie sonst über „weiter“.
      if (kursEndeLaden({ vorher, nachher: z.kursabgabe?.zustand, phase: z.phase })) void listenWeiter(true);
    } else abfragePlanen(); // fehlgeschlagen oder veraltet: der eigene Stand sagt noch „wird bewertet“
  }, ABFRAGE_MS);
}

function zusammenfassungZeigen() {
  // Nur im Listenmodus (auch am Kursende): Link auf die PDF-Route der Brücke.
  const sid = zusammenfassungSessionId(z.liste, z.listeEnde, z.turn);
  const link = $("zusammenfassung");
  link.hidden = sid === null;
  if (!link.hidden) link.href = `${API}/sitzungen/${encodeURIComponent(sid)}/zusammenfassung.pdf`;
}

function listenkopfZeigen() {
  zusammenfassungZeigen();
  const an = z.liste !== null && z.phase !== "ende";
  $("listenkopf").hidden = !an;
  $("kurs-abgeben").hidden = !kursAbgebenSichtbar({ jeAufgabe: z.jeAufgabe, phase: z.phase, kursabgabe: z.kursabgabe });
  if (!an) return;
  const aufgaben = z.liste.aufgaben;
  const wahl = $("aufgabe-wahl");
  // Nur neu aufbauen, wenn sich die Liste ändert: eine offene Auswahl klappt sonst bei jeder Abfrage zu.
  const kennung = aufgaben.map((a) => a.nummer).join(",");
  if (wahl.dataset.liste !== kennung) {
    wahl.replaceChildren(...aufgaben.map((a) => el("option", {
      value: String(a.nummer), text: `Aufgabe ${a.nummer} von ${aufgaben.length}`,
    })));
    wahl.dataset.liste = kennung;
  }
  wahl.value = String(z.gewaehlt);
  const a = gewaehlteAufgabe();
  const status = $("aufgabe-status");
  status.textContent = a ? statusText(a.status, a.meldung) : "";
  status.dataset.status = a?.status ?? "";
  $("warte-text").textContent = warteText(z.liste.wartend);
  $("kurs-abgabe-text").textContent = z.jeAufgabe ? kursAbgabeText(z.kursabgabe) : "";
}

// „Kurs abgeben“: erst die Rückfrage, dann übernimmt die Brücke; die Seite fragt `/stand` ab, solange der Lauf geht.
async function kursAbgeben() {
  const t = z.turn;
  if (!t || !z.liste || !z.jeAufgabe || z.kursAbgebenBusy || z.kursabgabe?.zustand === "laeuft") return;
  const a = gewaehlteAufgabe();
  const feld = document.querySelector('#karte textarea[name="antwort"]');
  const typ = (z.listenAufgaben.get(z.gewaehlt) ?? t.task)?.type;
  if (entwurfSperrt({ typ, status: a?.status, entwurf: feld?.value })) {
    banner(entwurfText(z.gewaehlt));
    return;
  }
  if (!window.confirm(KURS_ABGEBEN_RUECKFRAGE)) return;
  banner("");
  z.kursAbgebenBusy = true;
  const token = z.token;
  z.gueltigAb = neueAnfrage(); // eine Standabfrage von vor dem Aufruf kennt den Lauf noch nicht (kursabgabe: null)
  try {
    const r = await api("POST", `/sitzungen/${t.session_id}/kurs-abgeben`, {});
    if (token !== z.token) return;
    z.gueltigAb = neueAnfrage(); // auch eine Abfrage, die während des POST losging
    if (r.status === 202 && r.daten?.kursabgabe) {
      z.kursabgabe = r.daten.kursabgabe;
      abfragePlanen();
    } else {
      banner(meldungFuer(r.daten));
    }
    listeZeigen();
  } finally {
    z.kursAbgebenBusy = false;
  }
}

function listeZeigen() {
  listenkopfZeigen();
  leisteZeigen();
}

async function aufgabeWaehlen(nummer) {
  const t = z.turn;
  if (!t || !z.liste || !z.liste.aufgaben.some((a) => a.nummer === nummer)) return;
  const wechsel = z.gewaehlt !== nummer;
  z.gewaehlt = nummer;
  z.korrektur = null;
  z.hinweis = null;
  if (wechsel && z.phase === "aufgabe" && t.state === "active") { hilfeLeeren(); karteAufgabe(); }
  listeZeigen();
  const token = z.token;
  const r = await api("POST", `/sitzungen/${t.session_id}/zeigen`, { nummer });
  if (r.ok && token === z.token) dokumenteUebernehmen(r.daten); // auch ein eben angelegtes Canvas
  if (!r.ok && z.turn === t && z.gewaehlt === nummer) {
    z.hinweis = listenMeldung(r.daten);
    leisteZeigen();
  }
}

function aufgabeBlaettern(schritt) {
  const aufgaben = z.liste?.aufgaben ?? [];
  const i = aufgaben.findIndex((a) => a.nummer === z.gewaehlt);
  if (i < 0) return;
  void aufgabeWaehlen(aufgaben[(i + schritt + aufgaben.length) % aufgaben.length].nummer);
}

// Eine Fehlerantwort im Listenkurs: ruhiger Hinweis; mit `stand` (409) gilt danach der Stand der Brücke.
function listenFehler(r, anfrage) {
  if (r.daten?.stand) standAnwenden(r.daten.stand, anfrage);
  z.hinweis = listenMeldung(r.daten);
  listeZeigen();
}

// Abgeben sperrt nichts: das Bild wird aufgenommen, die Bewertung läuft im Hintergrund. Die Nummer gilt
// ab dem Klick als „in_bewertung“ (`sendend`); Abfragen von vor der Abgabe werden verworfen.
async function listenAbgeben() {
  const t = z.turn;
  const a = gewaehlteAufgabe();
  if (!t || !a || a.status === "in_bewertung" || a.status === "bestaetigt") return;
  const nummer = a.nummer;
  const vorher = { status: a.status, meldung: a.meldung };
  z.hinweis = null;
  z.korrektur = null;
  banner("");
  z.sendend.add(nummer);
  z.gueltigAb = neueAnfrage();
  z.liste = eintragSetzen(z.liste, nummer, { status: "in_bewertung", meldung: null });
  listeZeigen();
  const token = z.token;
  const r = await api("POST", `/sitzungen/${t.session_id}/abgeben`, { nummer });
  if (token !== z.token) return;
  dokumenteUebernehmen(r.daten);
  z.sendend.delete(nummer);
  z.gueltigAb = neueAnfrage(); // was vor dieser Antwort losging, kennt sie nicht
  // Eigene Antwort: ihr Stand gilt (202 und 409 tragen ihn); ohne Stand den vorigen Status der Nummer.
  if (!(r.daten?.stand && standAnwenden(r.daten.stand, Infinity)) && r.status !== 202) {
    z.liste = eintragSetzen(z.liste, nummer, vorher);
  }
  if (r.status === 202) {
    abfragePlanen();
    listeZeigen();
    void speichern(false);
    return;
  }
  z.hinweis = listenMeldung(r.daten);
  abfragePlanen(); // 409 laeuft_schon: die Nummer wird bewertet, die Seite fragt nach
  listeZeigen();
}

async function listenPasst() {
  const t = z.turn;
  const a = gewaehlteAufgabe();
  const ergebnis = listenErgebnis(a);
  if (!t || !ergebnis || z.busy) return;
  z.hinweis = null;
  banner("");
  sperren(true);
  const token = z.token;
  const nr = neueAnfrage();
  const r = await api("POST", `/sitzungen/${t.session_id}/passt`,
    { nummer: a.nummer, vorbewertung_id: ergebnis.vorbewertung_id });
  if (token !== z.token) return;
  sperren(false);
  if (!r.ok) { listenFehler(r, nr); return; }
  gebuchtUebernehmen(r.daten, nr, a.nummer);
  z.korrektur = null;
  zeigen(); // neue offene Aufgabe in der Seitenleiste, am Kursende die Bewertung
  void speichern(false);
  void notizenLaden();
}

// Nach Passt oder Antwort: Turn, Begriffe, Stand; die gewählte Aufgabe wird `weiter_zu`.
function gebuchtUebernehmen(daten, nr, nummer) {
  dokumenteUebernehmen(daten); // auch wenn der Stand veraltet ist: das Canvas von `weiter_zu` kann neu sein
  if (daten.turn) z.turn = daten.turn;
  if (Array.isArray(daten.begriffe)) begriffeZeigen(daten.begriffe);
  standAnwenden(daten, nr);
  const weiter = Number.isInteger(daten.weiter_zu) ? daten.weiter_zu : naechsteOffene(z.liste?.aufgaben, nummer);
  if (weiter !== null) z.gewaehlt = weiter;
}

async function listenKorrektur() {
  const t = z.turn;
  const a = gewaehlteAufgabe();
  const ergebnis = listenErgebnis(a);
  const task = a ? z.listenAufgaben.get(a.nummer) : null;
  if (!t || !ergebnis || !task || z.busy) return;
  const antwort = korrekturAntwort(task, $("korrektur-feld").value);
  if (!antwort) {
    z.hinweis = `${TEXT.nichtZugeordnet} ${eingabeHinweis(task.type) ?? ""}`.trim();
    leisteZeigen();
    return;
  }
  z.hinweis = null;
  sperren(true);
  const token = z.token;
  const nr = neueAnfrage();
  const r = await api("POST", `/sitzungen/${t.session_id}/korrektur`,
    { nummer: a.nummer, vorbewertung_id: ergebnis.vorbewertung_id, answer: antwort });
  if (token !== z.token) return;
  sperren(false);
  if (!r.ok) { listenFehler(r, nr); return; }
  dokumenteUebernehmen(r.daten);
  standAnwenden(r.daten.stand, nr);
  z.korrektur = null;
  // Karte nicht auf der Fläche (z. B. kein Platz): die Brücke schickt die Rückmeldung als Text.
  z.hinweis = typeof r.daten.karte === "string" && r.daten.karte ? r.daten.karte : null;
  listeZeigen();
  void speichern(false);
}

// Antwort aus der Seitenleiste (Typ `code`) für die gewählte Aufgabe; danach Rückmeldung, kein Auto-Weiter.
async function listenAntworten(antwort) {
  const t = z.turn;
  const nummer = z.gewaehlt;
  if (!t || nummer === null || z.busy) return;
  sperren(true);
  banner("");
  const token = z.token;
  const nr = neueAnfrage();
  const r = await api("POST", `/sitzungen/${t.session_id}/antwort`, { nummer, answer: antwort });
  if (token !== z.token) return;
  sperren(false);
  if (!r.ok) { banner(listenMeldung(r.daten)); listenFehler(r, nr); return; }
  gebuchtUebernehmen(r.daten, nr, nummer);
  const state = r.daten.turn?.feedback?.state ?? null;
  const text = typeof r.daten.rueckmeldung === "string" ? r.daten.rueckmeldung : "";
  if (!state && !text) { await listenWeiter(); return; }
  z.rueckmeldung = { state, text };
  z.phase = "rueckmeldung";
  zeigen();
  void notizenLaden();
}

/**
 * „Weiter“ im Listenkurs (auch nach einem 409 mit Turn): Stand neu laden, die Brücke holt `aktuell` ins
 * Bild. Die gewählte Aufgabe bleibt, wenn es sie noch offen gibt; sonst `aktuell`.
 */
async function listenWeiter(bannerBehalten = false) {
  const t = z.turn;
  if (!t) return;
  sperren(true);
  if (bannerBehalten !== true) banner("");
  const token = z.token;
  const nr = neueAnfrage();
  const r = await api("POST", `/sitzungen/${t.session_id}/weiter`);
  if (token !== z.token) return;
  sperren(false);
  if (!r.ok) { listenFehler(r, nr); return; }
  dokumenteUebernehmen(r.daten);
  z.rueckmeldung = null;
  z.phase = "aufgabe";
  neueAufgabe(bannerBehalten === true);
  if (r.daten.turn) z.turn = r.daten.turn;
  standAnwenden(r.daten, nr);
  const gewaehlt = gewaehlteAufgabe();
  if (!gewaehlt || gewaehlt.status === "bestaetigt") z.gewaehlt = r.daten.aktuell ?? z.liste?.aktuell ?? null;
  zeigen();
  // Die Brücke zeigt `aktuell`; ist eine andere Aufgabe gewählt, diese ins Bild holen.
  if (z.gewaehlt !== null && z.gewaehlt !== r.daten.aktuell && z.turn?.state === "active") {
    void api("POST", `/sitzungen/${z.turn.session_id}/zeigen`, { nummer: z.gewaehlt })
      .then((gezeigt) => { if (gezeigt.ok && token === z.token) dokumenteUebernehmen(gezeigt.daten); });
  }
  void speichern(false);
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

// Der vollständige Fall (das Brett zeigt ihn nur gekürzt) als aufklappbarer Block unter dem Aufgabentext.
function fallBlock(task, math) {
  const fall = task.presentation?.fall;
  if (typeof fall !== "string" || !fall.trim()) return null;
  return el("details", { class: "fall" }, el("summary", { text: TEXT.fallSeitenleiste }), setzeText(el("p"), fall, math));
}

// ---- Begriffs-Kärtchen: Antippen statt Schreiben ------------------------------------------------------
function kaertchenAufgabe() {
  const t = z.turn;
  if (!t || z.phase !== "aufgabe" || t.state !== "active") return null;
  const task = z.liste ? z.listenAufgaben.get(z.gewaehlt) ?? null : t.task;
  return task && KAERTCHEN_TYPEN.has(task.type) ? task : null;
}

function kaertchenGesperrt(task) {
  const gewaehlt = z.liste ? gewaehlteAufgabe() : null;
  const bewertet = gewaehlt !== null && (gewaehlt.status === "in_bewertung" || gewaehlt.status === "bestaetigt");
  return z.busy || bewertet || kaertchenAntwort(task, z.kaertchen?.zustand) === null;
}

function kaertchenKnopf(etikett, label, gedrueckt, onclick, badge) {
  const knopf = el("button", { type: "button", class: "kaertchen-knopf", "aria-pressed": gedrueckt ? "true" : "false", onclick });
  if (badge) knopf.append(el("span", { class: "kaertchen-nr", text: String(badge) }));
  knopf.append(setzeText(el("span"), `${etikett}) ${label}`, z.aufgabeMathe));
  return knopf;
}

// Baut die Kärtchen nur neu, wenn sich Auswahl oder Sperrzustand ändern (sonst verlieren Klicks beim Abfragen den Knopf).
function kaertchenAktualisieren() {
  const box = document.getElementById("kaertchen");
  const task = kaertchenAufgabe();
  if (!box || !task) return;
  if (z.kaertchen?.taskId !== task.id) z.kaertchen = { taskId: task.id, zustand: kaertchenStart(task) };
  const zu = z.kaertchen.zustand;
  const gesperrt = kaertchenGesperrt(task);
  const plan = `${kaertchenKennung(task, zu)}|${gesperrt ? 1 : 0}|${z.busy ? 1 : 0}`;
  if (box.dataset.plan !== plan) {
    let et;
    try {
      et = etiketten(task);
    } catch {
      box.replaceChildren();
      box.dataset.plan = plan;
      return;
    }
    const tippen = (etikett) => () => {
      z.kaertchen.zustand = kaertchenTippen(task, z.kaertchen.zustand, etikett);
      kaertchenAktualisieren();
    };
    const eintraege = (liste) => (Array.isArray(liste) ? liste : []);
    const labelZu = (liste, id) => eintraege(liste).find((e) => e?.id === id)?.label ?? "";
    const teile = [el("p", { class: "hinweistext", text: TEXT.kaertchenHinweis })];
    if (task.type === "matching") {
      const links = eintraege(task.presentation?.left);
      const rechts = eintraege(task.presentation?.right);
      for (const [nr, id] of Object.entries(et.links)) {
        const zeile = el("div", { class: "kaertchen-zeile" },
          setzeText(el("p", { class: "kaertchen-links" }), `${nr}) ${labelZu(links, id)}`, z.aufgabeMathe));
        const knoepfe = el("div", { class: "kaertchen-reihe" });
        for (const b of Object.keys(et.rechts)) {
          knoepfe.append(el("button", {
            type: "button", class: "kaertchen-knopf", "aria-pressed": zu.paare[nr] === b ? "true" : "false", text: b,
            onclick: () => {
              z.kaertchen.zustand = kaertchenPaar(z.kaertchen.zustand, nr, b);
              kaertchenAktualisieren();
            },
          }));
        }
        zeile.append(knoepfe);
        teile.push(zeile);
      }
      const legende = el("ul", { class: "kaertchen-legende" });
      for (const [b, id] of Object.entries(et.rechts)) {
        legende.append(setzeText(el("li"), `${b}) ${labelZu(rechts, id)}`, z.aufgabeMathe));
      }
      teile.push(legende);
    } else {
      const reihe = el("div", { class: "kaertchen-reihe" });
      for (const [b, id] of Object.entries(et.optionen)) {
        const nr = task.type === "ordering" ? zu.wahl.indexOf(b) + 1 : 0;
        reihe.append(kaertchenKnopf(b, labelZu(task.options, id), zu.wahl.includes(b), tippen(b), nr || null));
      }
      teile.push(reihe);
    }
    teile.push(el("button", {
      type: "button", id: "kaertchen-abgeben", class: "haupt-knopf", text: TEXT.kaertchenAbgeben,
      disabled: gesperrt, onclick: () => void kaertchenAbgeben(),
    }));
    box.replaceChildren(...teile);
    box.dataset.plan = plan;
  }
  // `sperren` setzt alle Knöpfe der Karte; hier gilt wieder der echte Stand.
  const abgeben = document.getElementById("kaertchen-abgeben");
  if (abgeben) abgeben.disabled = gesperrt;
}

async function kaertchenAbgeben() {
  const t = z.turn;
  const task = kaertchenAufgabe();
  if (!t || !task || z.busy || kaertchenGesperrt(task)) return;
  const antwort = kaertchenAntwort(task, z.kaertchen?.zustand);
  if (!antwort) return;
  z.hinweis = null;
  banner("");
  if (z.liste) {
    const a = gewaehlteAufgabe();
    if (!a) return;
    sperren(true);
    const token = z.token;
    const nr = neueAnfrage();
    const r = await api("POST", `/sitzungen/${t.session_id}/waehlen`, { nummer: a.nummer, answer: antwort });
    if (token !== z.token) return;
    sperren(false);
    if (!r.ok) { listenFehler(r, nr); return; }
    dokumenteUebernehmen(r.daten);
    standAnwenden(r.daten.stand, nr);
    z.korrektur = null;
    z.kaertchen = null;
    // Karte nicht auf der Fläche (z. B. kein Platz): die Brücke schickt die Rückmeldung als Text.
    z.hinweis = typeof r.daten.karte === "string" && r.daten.karte ? r.daten.karte : null;
    listeZeigen();
    void speichern(false);
    return;
  }
  sperren(true);
  const r = await api("POST", `/sitzungen/${t.session_id}/waehlen`, { revision: t.session_revision, answer: antwort });
  if (r.ok) z.kaertchen = null;
  ergebnisUebernehmen(r);
  kaertchenAktualisieren();
}

function karteAufgabe() {
  const t = z.turn;
  // Listenkurs: die Karte gilt der gewählten Aufgabe (Antwortfeld für Typ `code`, `{nummer, answer}`).
  const task = (z.liste ? z.listenAufgaben.get(z.gewaehlt) : null) ?? t.task;
  const math = task.presentation?.math === true;
  z.aufgabeMathe = math;
  const sperrText = sperrHinweis(t);
  // „Erklär mir das“ und „Hinweis“ fragen die Brücke zur Aufgabe des Turns; nur bei dieser anbieten.
  const hilfeKnoepfe = !z.liste || task.id === t.task?.id ? [
    el("button", { type: "button", id: "erklaeren", text: "Erklär mir das", onclick: () => erklaeren() }),
    el("button", { type: "button", id: "hinweis", text: "Hinweis", onclick: hinweis }),
  ] : [];
  const sperre = sperrText ? el("p", { class: "hinweistext sperrhinweis", text: sperrText }) : null;
  if (z.liste && task.type === "code" && gewaehlteAufgabe()?.status === "bestaetigt") {
    $("karte").replaceChildren(
      setzeText(el("p", { class: "aufgabe" }), task.prompt, math),
      el("p", { class: "hinweistext", text: "Diese Aufgabe ist schon bestätigt." }));
    return;
  }
  if (task.type !== "code") {
    // Alles andere wird auf der Fläche beantwortet; Abgeben, Passt usw. sitzen in der Leiste über der Fläche.
    $("karte").replaceChildren(...ohneLeere(
      setzeText(el("p", { class: "aufgabe" }), task.prompt, math),
      fallBlock(task, math),
      el("p", { class: "hinweistext", text: TEXT.aufFlaeche }),
      KAERTCHEN_TYPEN.has(task.type) ? el("section", { id: "kaertchen", class: "kaertchen", "aria-label": "Kärtchen" }) : null,
      sperre,
      el("div", { class: "knoepfe" }, ...hilfeKnoepfe)));
    kaertchenAktualisieren();
    return;
  }
  const feld = el("textarea", { name: "antwort", required: true, placeholder: "Lösung und Begründung eingeben" });
  const form = el("form", { novalidate: false },
    setzeText(el("p", { class: "aufgabe" }), task.prompt, math),
    fallBlock(task, math),
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
    type: "button", text: "Noch einmal üben", hidden: z.gruppeId !== null, // die Gruppe hat keinen neuen Durchlauf
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
  autoVerbindenPruefen();
}

// PenEcho kann „bereit“ melden, bevor die Brücke „nicht verbunden“ antwortet (und umgekehrt): beide Reihenfolgen
// führen zu höchstens einem automatischen Verbinden je Laden.
function autoVerbindenPruefen() {
  if (!autoVerbindenFaellig({ mcpBereit: z.mcpBereit, hinweisSichtbar: !$("flaechen-hinweis").hidden, schonAutoVerbunden: z.mcpAutoVerbunden })) return;
  z.mcpAutoVerbunden = true;
  void flaecheVerbinden();
}

// Erneutes Oeffnen des aktuellen Kurses nur zum Pruefen der Verbindung: nichts wird abgegeben oder neu geladen.
async function flaecheVerbinden() {
  const kurs = z.kurs;
  if (!kurs) return;
  const nr = neueAnfrage();
  const r = await api("POST", oeffnenPfad(kurs));
  if (z.kurs !== kurs) return;
  if (!r.ok) { fehlerBehandeln(r); return; }
  if (z.gruppeId !== null && await gruppeUebernehmen(r.daten.gruppe)) { await kursOeffnen(kurs, "oeffnen", false); return; }
  flaechenHinweisZeigen(r.daten);
  if (z.liste && standAnwenden(r.daten.stand, nr)) listeZeigen();
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
  if (z.liste) {
    // Listenkurs: kein alter Weiter-Weg; der Stand kommt neu von der Brücke (sie zeigt `aktuell`).
    banner(listenMeldung(r.daten));
    if (r.status === 409 && (r.daten?.turn || r.daten?.stand)) void listenWeiter(true);
    return;
  }
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

// ---- Gruppenfläche (Teil C) ------------------------------------------------------------------------
function oeffnenPfad(kurs, aktion = "oeffnen") {
  if (z.gruppeId !== null) return `/gruppen/${z.gruppeId}/oeffnen`;
  return `/kurse/${kurs.course_id}/${aktion === "neu" ? "neu" : "oeffnen"}`;
}

function gemerkteRahmenGruppe(id) {
  try {
    const w = JSON.parse(window.sessionStorage.getItem(GRUPPE_SCHLUESSEL + id) ?? "null");
    if (w && w.gruppe === `g-${id}` && typeof w.person === "string") return { gruppe: w.gruppe, person: w.person };
  } catch {
    // gesperrter Speicher oder kaputter Wert: die Fläche lädt nach dem ersten Öffnen
  }
  return null;
}

function gruppenKopfZeigen(name) {
  const wahl = $("kurs").closest("label");
  wahl.hidden = true; // der Kurs gehört zur Gruppe, es gibt nichts zu wählen
  let kopf = $("gruppe-name");
  if (!kopf) {
    kopf = el("p", { id: "gruppe-name", class: "gruppe-name" });
    wahl.after(kopf);
  }
  kopf.textContent = `Gruppe: ${typeof name === "string" ? name : ""}`;
}

// Gruppe und Person aus der Antwort übernehmen. Ist das neu, lädt die Fläche mit `&gruppe=&person=` (die Person
// kommt nur von der Brücke) und meldet sich danach beim MCP; `true` heißt: einmal neu öffnen.
async function gruppeUebernehmen(g) {
  if (!g || typeof g !== "object") return false;
  gruppenKopfZeigen(g.gruppe_name);
  const neu = { gruppe: g.flaeche, person: g.person };
  const alt = z.rahmenGruppe;
  if (alt && alt.gruppe === neu.gruppe && alt.person === neu.person) return false;
  z.rahmenGruppe = neu;
  try {
    window.sessionStorage.setItem(GRUPPE_SCHLUESSEL + z.gruppeId, JSON.stringify(neu));
  } catch {
    // ohne Speicher lädt die Fläche nach jedem Neuladen einmal mehr
  }
  kennung = await flaecheErneuern({ rahmen: $("rahmen"), gruppe: z.rahmenGruppe });
  await pause(VERBINDEN_MS);
  return true;
}

// `aktion` "neu" startet nach dem Kursende bewusst einen frischen Durchlauf. `rahmenNeu`: in der Gruppe darf
// eine neu geladene Fläche einmal ein zweites Öffnen auslösen, nie mehr.
async function kursOeffnen(kurs, aktion = "oeffnen", rahmenNeu = true) {
  banner("");
  $("flaechen-hinweis").hidden = true;
  z.kurs = kurs;
  z.token += 1;
  z.speicherHinweis = null;
  z.turn = null;
  z.phase = "aufgabe";
  z.rueckmeldung = null;
  abfrageStoppen();
  z.liste = null;
  z.listeEnde = false;
  z.jeAufgabe = false;
  z.dokumente = {};
  z.nachladen = null;
  z.kursabgabe = null;
  z.listenAufgaben = new Map();
  z.gewaehlt = null;
  z.verworfen = new Set();
  z.sendend = new Set();
  z.gueltigAb = 0;
  neueAufgabe();
  zeigen();
  begriffeZeigen([]);
  $("notizen-box").hidden = true;
  const nr = neueAnfrage();
  const r = await api("POST", oeffnenPfad(kurs, aktion));
  if (z.kurs !== kurs) return;
  if (!r.ok) { fehlerBehandeln(r); return; }
  if (z.gruppeId !== null) {
    if (r.daten.kurs) Object.assign(kurs, r.daten.kurs); // Titel und Kennung des Kurses der Gruppe
    if (await gruppeUebernehmen(r.daten.gruppe) && rahmenNeu) { await kursOeffnen(kurs, aktion, false); return; }
    if (z.kurs !== kurs) return;
  }
  begriffeZeigen(r.daten.begriffe);
  turnUebernehmen(r.daten.turn, r.daten.nummer);
  // Listenkurs nur, wenn die Brücke es sagt; anpassende Kurse behalten den bisherigen Kopf.
  if (r.daten.abgeschlossen === true && r.daten.stand?.ablauf === "liste") {
    z.listeEnde = true; // nur der Link; die Ansicht des beendeten Kurses bleibt wie sie ist
  } else if (r.daten.stand?.ablauf === "liste") {
    for (const e of Array.isArray(r.daten.aufgaben) ? r.daten.aufgaben : []) {
      if (Number.isInteger(e?.nummer) && e.task) z.listenAufgaben.set(e.nummer, e.task);
    }
    z.gewaehlt = Number.isInteger(r.daten.nummer) ? r.daten.nummer : r.daten.stand.aktuell;
    standAnwenden(r.daten.stand, nr);
  }
  flaechenHinweisZeigen(r.daten);
  if (r.daten.flaeche === false && !flaechenHinweis(r.daten)) banner(TEXT.flaecheWeg);
  zeigen();
  void notizenLaden();
}

async function antwortSenden(antwort) {
  if (z.liste) { await listenAntworten(antwort); return; }
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
  if (z.liste) { await listenWeiter(bannerBehalten); return; }
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
  kaertchenAktualisieren();
  // Listenkurs: die Leiste gilt der gewählten Aufgabe; ihre Knöpfe folgen deren Stand.
  const gewaehlt = z.liste ? gewaehlteAufgabe() : null;
  const task = z.liste ? z.listenAufgaben.get(z.gewaehlt) ?? null : t?.task;
  const ergebnis = z.liste ? listenErgebnis(gewaehlt) : z.ergebnis;
  const zustand = z.phase === "aufgabe" && t?.state === "active"
    ? leistenZustand(task, ergebnis)
    : { sichtbar: false, knoepfe: [] };
  leiste.hidden = !zustand.sichtbar;
  if (!zustand.sichtbar) return;
  const abgebenGesperrt = gewaehlt !== null && (gewaehlt.status === "in_bewertung" || gewaehlt.status === "bestaetigt");
  const aktion = {
    speichern: () => { if (!z.speichert) void speichern(true); },
    abgeben: () => void abgeben(),
    passt: () => void passt(),
    ueberarbeiten: () => ueberarbeiten(),
    falsch: () => korrekturOeffnen("falsch"),
    eintippen: () => korrekturOeffnen("eintippen"),
  };
  // Nur bei echter Änderung neu bauen; die Aktionen lesen den Zustand erst beim Klick.
  const plan = leistenKnoepfe(zustand.knoepfe, { busy: z.busy, speichert: z.speichert !== null, abgebenGesperrt });
  const knoepfeBox = $("leisten-knoepfe");
  if (knoepfeBox.dataset.plan !== plan.kennung) {
    knoepfeBox.replaceChildren(...plan.knoepfe.map((k) => el("button", {
      type: "button", id: `leiste-${k.id}`, class: k.haupt ? "haupt-knopf" : "",
      disabled: k.disabled, text: k.text, onclick: aktion[k.id],
    })));
    knoepfeBox.dataset.plan = plan.kennung;
  }
  const text = z.busy ? TEXT.wirdBewertet : (z.hinweis ?? z.speicherHinweis ?? ergebnisHinweis(ergebnis));
  $("leisten-hinweis").textContent = text ?? "";
  $("leisten-hinweis").hidden = !text;
  const offen = z.korrektur !== null && ergebnis !== null && zustand.knoepfe.includes(z.korrektur === "falsch" ? "falsch" : "eintippen");
  $("korrektur").hidden = !offen;
  if (offen) $("korrektur-text").textContent = eingabeHinweis(task.type) ?? "";
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
  const a = z.liste ? gewaehlteAufgabe() : null;
  if (typeof a?.vorbewertung_id === "string") z.verworfen.add(a.vorbewertung_id);
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
  if (z.liste) { await listenAbgeben(); return; }
  const t = z.turn;
  if (!t || z.busy) return;
  z.hinweis = null;
  banner("");
  sperren(true);
  ergebnisUebernehmen(await api("POST", `/sitzungen/${t.session_id}/abgeben`, { revision: t.session_revision }));
}

// ---- Nachrichten aus der Fläche (PenEcho im iframe) -------------------------------------------------------
// Zustand für `abgabeNummerZulaessig`: nur die eigene Sitzung (Gruppe: eigene Mappe), nie die einer anderen Person.
function abgabeZustand() {
  const t = z.turn;
  if (z.liste) return { liste: true, aufgaben: z.liste.aufgaben };
  const aktiv = t && z.phase === "aufgabe" && t.state === "active";
  return { liste: false, aktuell: aktiv ? (z.nummer ?? (t.progress?.completed ?? 0) + 1) : null };
}

// Aufgabe zu einer Nachricht der Fläche (`nummerZurNachricht`): im alten Modell wie bisher über den Bereich, je Aufgabe
// nur über das Dokument.
const nummerSofort = (m) => nummerZurNachricht(
  { jeAufgabe: z.jeAufgabe, dokumente: z.dokumente, dokument: m.dokument, bereich: m.bereich });

// Wie `nummerSofort`; ist das Dokument je Aufgabe unbekannt (Canvas nach dem letzten Stand angelegt), wird die
// Zuordnung einmal neu geladen. null: gehört zu keiner Aufgabe dieses Kurses.
async function nummerAusNachricht(m) {
  const nummer = nummerSofort(m);
  if (nummer !== null || !z.jeAufgabe) return nummer;
  await dokumenteNachladen();
  return nummerSofort(m);
}

// Ein Canvas je Aufgabe: `dokumente` neu von der Brücke (`/stand`); gleichzeitige Nachrichten teilen die Anfrage.
function dokumenteNachladen() {
  if (z.nachladen) return z.nachladen;
  const t = z.turn;
  if (!t) return Promise.resolve();
  const token = z.token;
  const laden = api("GET", `/sitzungen/${t.session_id}/stand`)
    .then((r) => { if (r.ok && token === z.token) dokumenteUebernehmen(r.daten); })
    .catch(() => {})
    .finally(() => { if (z.nachladen === laden) z.nachladen = null; });
  z.nachladen = laden;
  return laden;
}

// Die Fläche zeigt ein anderes Dokument: die gewählte Aufgabe folgt, ohne `/zeigen`. Je Aufgabe lädt ein unbekanntes
// Dokument die Zuordnung einmal neu; bleibt es unbekannt, kommt ein Hinweis (nicht am Kursende: dort ist das
// Kurs-Canvas vorn, es gehört zu keiner Aufgabe).
async function dokumentAktiv(dokument) {
  let nummer = nummerAusDokument(z.dokumente, dokument);
  if (nummer === null && z.jeAufgabe && z.liste && z.turn && z.phase !== "ende" && z.kursabgabe?.zustand !== "fertig") {
    const token = z.token;
    nummer = await nummerAusNachricht({ dokument });
    if (token !== z.token) return;
    if (nummer === null) {
      z.hinweis = FLAECHE_OHNE_AUFGABE;
      leisteZeigen();
      return;
    }
  }
  const hinweis = hinweisFuerAktiv(z.hinweis, nummer);
  if (hinweis !== z.hinweis) {
    z.hinweis = hinweis;
    leisteZeigen();
  }
  if (nummer === null || !z.liste || !z.turn || z.gewaehlt === nummer) return;
  if (!z.liste.aufgaben.some((a) => a.nummer === nummer)) return;
  z.gewaehlt = nummer;
  z.korrektur = null;
  z.hinweis = null;
  if (z.phase === "aufgabe" && z.turn.state === "active") { hilfeLeeren(); karteAufgabe(); }
  listeZeigen();
}

// „Ich gebe ab“ / Knopf in PenEcho: dieselbe Funktion wie der Knopf „Abgeben“ der Leiste (mit ihrer Sperre).
async function flaecheAbgeben(nummer) {
  const t = z.turn;
  if (!t || z.flaechenAbgabe || z.busy) return;
  if (!abgabeNummerZulaessig(nummer, abgabeZustand())) {
    z.hinweis = TEXT.abgabeUnzulaessig;
    leisteZeigen();
    return;
  }
  z.flaechenAbgabe = true;
  try {
    if (z.liste && z.gewaehlt !== nummer) await aufgabeWaehlen(nummer);
    // z.turn wird bei jeder Standabfrage ersetzt; maßgeblich sind Sitzung und gewählte Aufgabe, nicht die Objektidentität.
    if (z.turn?.session_id !== t.session_id || (z.liste && z.gewaehlt !== nummer)) {
      z.hinweis = TEXT.abgabeUnzulaessig;
      leisteZeigen();
      return;
    }
    await abgeben();
  } finally {
    z.flaechenAbgabe = false;
  }
}

// „Ich gebe ab“ in PenEcho: die Nummer kommt aus der Nachricht. Je Aufgabe nie über den Bereich: gehört die Fläche
// auch nach dem Nachladen zu keiner Aufgabe, nur der Hinweis und keine Abgabe.
async function flaecheAbgebenAus(m) {
  const sofort = nummerSofort(m);
  if (sofort !== null) { await flaecheAbgeben(sofort); return; }
  const token = z.token;
  const nummer = await nummerAusNachricht(m);
  if (token !== z.token) return;
  if (nummer === null) {
    z.hinweis = FLAECHE_OHNE_AUFGABE;
    leisteZeigen();
    return;
  }
  await flaecheAbgeben(nummer);
}

// Fire-and-forget: das Protokoll der KI-Schritte darf die Fläche nie stören. Gehört die Fläche zu keiner Aufgabe
// dieses Kurses, wird der Schritt still verworfen (nie unter Aufgabe 1 gebucht).
async function kiSchrittMelden(m) {
  const t = z.turn;
  if (!t) return;
  const nummer = await nummerAusNachricht(m);
  if (nummer === null) return;
  if (z.turn?.session_id !== t.session_id) return;
  void api("POST", `/sitzungen/${t.session_id}/ki-schritt`,
    { nummer, aktion: m.aktion, ausloeser: m.ausloeser }).catch(() => {});
}

function flaechenNachricht(ereignis) {
  const rahmen = $("rahmen");
  const m = nachrichtAusFlaeche(ereignis, rahmen.contentWindow, flaechenHerkunft(rahmen));
  if (!m) return;
  if (m.typ === "huelle-abgeben") void flaecheAbgebenAus(m);
  else if (m.typ === "huelle-ki-schritt") void kiSchrittMelden(m);
  else if (m.typ === "huelle-dokument-aktiv") void dokumentAktiv(m.dokument);
  else if (m.typ === "huelle-mcp-bereit") {
    z.mcpBereit = true;
    autoVerbindenPruefen();
  }
}

async function korrekturSenden() {
  if (z.liste) { await listenKorrektur(); return; }
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
  if (z.liste) { await listenPasst(); return; }
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
  // Die Fläche lädt erst mit der Kennung; so meldet sich dieser Tab gleich mit ihr als canvasId beim MCP.
  const rahmen = $("rahmen");
  // Gruppe: die Fläche lädt nur mit Gruppe und Person (gemerkt vom letzten Öffnen in diesem Tab), sonst erst nach
  // der Antwort der Brücke. Ohne `?gruppe=` genau wie bisher.
  if (z.gruppeId !== null) {
    z.rahmenGruppe = gemerkteRahmenGruppe(z.gruppeId);
    if (z.rahmenGruppe && rahmen.dataset.src) {
      rahmen.src = flaechenAdresse(rahmen.dataset.src, kennung, undefined, z.rahmenGruppe);
    }
  } else if (rahmen.dataset.src) rahmen.src = flaechenAdresse(rahmen.dataset.src, kennung);
  layoutSetzen(speicher("lesen", "huelle-layout"));
  window.addEventListener("message", flaechenNachricht);
  $("flaeche-verbinden").addEventListener("click", () => { void flaecheVerbinden(); });
  $("korrektur").addEventListener("submit", (ereignis) => { ereignis.preventDefault(); void korrekturSenden(); });
  $("layout-a").addEventListener("click", () => layoutSetzen("a"));
  $("layout-c").addEventListener("click", () => layoutSetzen("c"));
  $("aufgabe-zurueck").addEventListener("click", () => aufgabeBlaettern(-1));
  $("aufgabe-vor").addEventListener("click", () => aufgabeBlaettern(1));
  $("kurs-abgeben").addEventListener("click", () => { void kursAbgeben(); });
  $("aufgabe-wahl").addEventListener("change", (ereignis) => { void aufgabeWaehlen(Number(ereignis.target.value)); });
  if (z.gruppeId !== null) {
    // Gruppe: keine Kurswahl; Kurs und Titel kommen aus der Antwort der Gruppenroute.
    await kursOeffnen({ course_id: null, titel: "" });
    return;
  }
  const r = await api("GET", "/kurse");
  if (!r.ok || !Array.isArray(r.daten) || r.daten.length === 0) {
    banner(r.ok ? TEXT.keinKurs : meldungFuer(r.daten));
    return;
  }
  z.kurse = r.daten;
  const wahl = $("kurs");
  wahl.replaceChildren(...z.kurse.map((k) => el("option", { value: k.course_id, text: k.titel })));
  // `?kurs=` aus der Übergabe der Kursübersicht wählt vor; sonst der gemerkte Kurs.
  wahl.value = kursVorwahl(location.search, z.kurse, speicher("lesen", "huelle-kurs"));
  wahl.addEventListener("change", () => {
    speicher("schreiben", "huelle-kurs", wahl.value);
    void kursOeffnen(z.kurse.find((k) => k.course_id === wahl.value));
  });
  await kursOeffnen(z.kurse.find((k) => k.course_id === wahl.value) ?? z.kurse[0]);
}

start();
