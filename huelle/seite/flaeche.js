// Seitenseite des Speicherns: die Huelle bittet die eingebettete Flaeche, PenEchos eigenen Knopf
// „Speichern“ zu druecken. Gegenstueck: `huelle/flaeche/speichern.js`, vom Durchreicher in PenEchos
// Seite eingefuegt. Knoepfe und Abgabe-Haken rufen nur `flaecheSpeichern(iframe)`.

export const ZEITLIMIT_MS = 15000; // laenger als das Zeitlimit der Flaeche (10 s)

// Teil B, eigene Flaeche je Person: jeder Tab hat eine Flaechen-Kennung lf-<32 hex>. Sie geht als `&flaeche=` in die
// Adresse der Flaeche (die meldet sich damit als canvasId beim MCP) und als Kopf X-Lernflaeche an jede API-Anfrage.
// sessionStorage gilt je Tab und ueberlebt das Neuladen; so bleibt die Flaeche eines Tabs dieselbe.
export const KENNUNG_SCHLUESSEL = "lernflaeche-kennung";
const KENNUNG_MUSTER = /^lf-[0-9a-f]{32}$/;

export function flaechenKennung({ speicher, zufall } = {}) {
  try {
    // Schon der Zugriff auf sessionStorage wirft, wenn der Browser Speicher sperrt.
    if (speicher === undefined) speicher = globalThis.sessionStorage;
    const alt = speicher?.getItem(KENNUNG_SCHLUESSEL);
    if (typeof alt === "string" && KENNUNG_MUSTER.test(alt)) return alt;
  } catch {
    // gesperrter Speicher: dann gilt die Kennung nur bis zum Neuladen
  }
  return neueFlaechenKennung({ speicher, zufall });
}

// Immer eine neue Kennung (auch wenn die gespeicherte gueltig ist) und in sessionStorage ablegen.
export function neueFlaechenKennung({ speicher, zufall = (feld) => globalThis.crypto.getRandomValues(feld) } = {}) {
  const feld = new Uint8Array(16);
  zufall(feld);
  const neu = "lf-" + Array.from(feld, (b) => b.toString(16).padStart(2, "0")).join("");
  try {
    if (speicher === undefined) speicher = globalThis.sessionStorage;
    speicher?.setItem(KENNUNG_SCHLUESSEL, neu);
  } catch {
    // gesperrter Speicher: die Kennung gilt nur bis zum Neuladen
  }
  return neu;
}

// Teil C, Gruppenflaeche: die Seite liest `?gruppe=<id>` (1 bis 9 Ziffern, ohne fuehrende Null, genau einmal).
const GRUPPE_ID_MUSTER = /^[1-9][0-9]{0,8}$/;
const GRUPPE_MUSTER = /^g-[1-9][0-9]{0,8}$/;
const PERSON_MUSTER = /^p[0-9a-f]{24}$/;

export function gruppeAusAdresse(suche) {
  try {
    const werte = new URLSearchParams(String(suche ?? "")).getAll("gruppe");
    return werte.length === 1 && GRUPPE_ID_MUSTER.test(werte[0]) ? Number(werte[0]) : null;
  } catch {
    return null;
  }
}

// Gruppe und Person fuer die Adresse der Flaeche; die Person kommt aus der Antwort der Bruecke.
function gruppenTeile(gruppe) {
  if (!gruppe || typeof gruppe !== "object") return null;
  const { gruppe: flaeche, person } = gruppe;
  if (typeof flaeche !== "string" || !GRUPPE_MUSTER.test(flaeche)) return null;
  if (typeof person !== "string" || !PERSON_MUSTER.test(person)) return null;
  return { flaeche, person };
}

// `basis` darf relativ sein (data-src="/?lernen=1" ohne eigene Flaechenadresse): sie wird gegen die Adresse der
// Seite aufgeloest, damit die Kennung immer in der URL steht. Mit gueltiger `gruppe` ({gruppe: "g-<id>", person})
// kommen `&gruppe=` und `&person=` dazu: die Flaeche verbindet sich dann mit dem Abgleich der Gruppe.
export function flaechenAdresse(basis, kennung, herkunft = globalThis.location?.href, gruppe = null) {
  if (typeof kennung !== "string" || !KENNUNG_MUSTER.test(kennung)) return basis;
  try {
    const url = new URL(basis, herkunft);
    url.searchParams.set("flaeche", kennung);
    const teile = gruppenTeile(gruppe);
    if (teile) {
      url.searchParams.set("gruppe", teile.flaeche);
      url.searchParams.set("person", teile.person);
    }
    return url.toString();
  } catch {
    return basis;
  }
}

export function flaechenKopf(kennung) {
  return typeof kennung === "string" && KENNUNG_MUSTER.test(kennung) ? { "X-Lernflaeche": kennung } : {};
}

// Fix-Runde 1: gehoert die Kennung des Tabs einer anderen Person (Personenwechsel im selben Tab), antwortet die
// Bruecke wie ohne Flaeche und setzt den Kopf `X-Lernflaeche-Kennung: belegt`. Die Seite vergibt dann eine neue
// Kennung, laedt die Flaeche mit ihr neu und wiederholt die ausloesende Anfrage hoechstens einmal.
export const BELEGT_KOPF = "x-lernflaeche-kennung";
export const WARTEN_MS = 10000; // laengstens so lange auf das Neuladen der Flaeche warten

export function kennungBelegt(antwort) {
  try {
    return antwort?.headers?.get?.(BELEGT_KOPF) === "belegt";
  } catch {
    return false;
  }
}

export function flaecheErneuern({ rahmen, speicher, zufall, wartenMs = WARTEN_MS, gruppe = null } = {}) {
  const kennung = neueFlaechenKennung({ speicher, zufall });
  if (!rahmen?.dataset?.src) return Promise.resolve(kennung);
  return new Promise((fertig) => {
    let uhr = null;
    const geladen = () => {
      clearTimeout(uhr);
      rahmen.removeEventListener("load", geladen);
      fertig(kennung);
    };
    uhr = setTimeout(geladen, wartenMs);
    rahmen.addEventListener("load", geladen);
    rahmen.src = flaechenAdresse(rahmen.dataset.src, kennung, undefined, gruppe);
  });
}

// Nur Anfragen, die nichts buchen, werden wiederholt (lesen, oeffnen, weiter, zeigen); eine Antwort oder Abgabe
// ginge sonst doppelt ein oder liefe gegen die frische, leere Flaeche. Dort reicht die neue Kennung fuer den
// naechsten Versuch der Person.
export function wiederholbar(methode, pfad) {
  if (methode === "GET") return true;
  return /\/(oeffnen|weiter|zeigen)$/.test(String(pfad).split("?")[0]);
}

export async function mitFlaechenKennung(senden, { erneuern, wiederholbar: nochmal }) {
  const erst = await senden();
  if (!erst?.belegt) return erst;
  await erneuern();
  if (!nochmal) return erst;
  const zweit = await senden();
  if (zweit?.belegt) await erneuern(); // keine zweite Wiederholung; die Seite zeigt „Flaeche verbinden“
  return zweit;
}

// `code` (maschinenlesbar, die Seite entscheidet nur danach): gespeichert | geaendert | leer | kein_knopf |
// zeitueberschreitung | fehler (von der Flaeche, siehe huelle/flaeche/speichern.js) sowie keine_flaeche
// (kein Rahmen) und keine_antwort (Zeitlimit ohne Antwort) von hier. `grund` bleibt Klartext.
export const CODES = new Set(["gespeichert", "geaendert", "leer", "kein_knopf", "zeitueberschreitung", "fehler", "keine_flaeche", "keine_antwort"]);
const KEINE_FLAECHE = { ok: false, grund: "keine Fläche", code: "keine_flaeche" };

function codeAus(d) {
  if (d.ok === true) return "gespeichert";
  return typeof d.code === "string" && CODES.has(d.code) && d.code !== "gespeichert" ? d.code : "fehler";
}

function herkunftVon(iframe) {
  try {
    const herkunft = new URL(iframe.src).origin;
    return herkunft && herkunft !== "null" ? herkunft : null;
  } catch {
    return null;
  }
}

/**
 * Loest das Speichern in der Flaeche aus. Loest immer auf, nie mit Fehler:
 * `{ok: true, grund: null}` oder `{ok: false, grund}`; ohne Antwort nach dem Zeitlimit "keine Antwort".
 */
export function flaecheSpeichern(iframe, {
  zeitlimitMs = ZEITLIMIT_MS, fenster = globalThis.window, neueId = () => globalThis.crypto.randomUUID(),
} = {}) {
  const ziel = iframe ? herkunftVon(iframe) : null;
  const kind = iframe?.contentWindow;
  if (!ziel || !kind || !fenster) return Promise.resolve({ ...KEINE_FLAECHE });
  const id = neueId();
  return new Promise((fertig) => {
    let uhr = null;
    const ende = (ergebnis) => {
      clearTimeout(uhr);
      fenster.removeEventListener("message", hoerer);
      fertig(ergebnis);
    };
    function hoerer(ereignis) {
      if (ereignis.source !== kind || ereignis.origin !== ziel) return;
      const d = ereignis.data;
      if (!d || typeof d !== "object" || d.typ !== "huelle-gespeichert" || d.id !== id) return;
      ende(d.ok === true ? { ok: true, grund: null, code: "gespeichert" } : { ok: false, grund: String(d.grund || "PenEcho hat nicht gespeichert"), code: codeAus(d) });
    }
    fenster.addEventListener("message", hoerer);
    uhr = setTimeout(() => ende({ ok: false, grund: "keine Antwort", code: "keine_antwort" }), zeitlimitMs);
    try {
      kind.postMessage({ typ: "huelle-speichern", id }, ziel);
    } catch (fehler) {
      ende({ ok: false, grund: `Senden fehlgeschlagen: ${String(fehler?.message ?? fehler)}`, code: "fehler" });
    }
  });
}

// ---- Nachrichten der Flaeche an die Huelle ---------------------------------------------------------
// PenEcho meldet vier Dinge an `window.parent`: huelle-abgeben, huelle-ki-schritt, huelle-mcp-bereit, huelle-dokument-aktiv. PenEcho kennt
// sein Ziel nur ungefaehr (ancestorOrigins, Referrer, sonst "*"); deshalb prueft die Huelle hier selbst: die Quelle
// muss der eigene Flaechen-iframe sein und die Herkunft die der Flaechen-Adresse (wie beim Speichern).
const BEREICH_MAX = 499; // Route ki-schritt: nummer = bereich + 1, hoechstens 500
const AUSLOESER = new Set(["manuell", "auto"]);

/** PenEchos documentId: kurzer Bezeichner aus festen Zeichen. */
export function dokumentGueltig(d) {
  return typeof d === "string" && /^[A-Za-z0-9_.:-]{1,256}$/.test(d);
}

function bereichGueltig(b) {
  return Number.isInteger(b) && b >= 0 && b <= BEREICH_MAX;
}

/** `{typ, ...}` (normalisiert) oder null, wenn Quelle, Herkunft, Typ oder Felder nicht stimmen. */
export function nachrichtAusFlaeche(ereignis, erwarteteQuelle, erwarteteHerkunft) {
  if (!ereignis || !erwarteteQuelle || !erwarteteHerkunft) return null;
  if (ereignis.source !== erwarteteQuelle || ereignis.origin !== erwarteteHerkunft) return null;
  const d = ereignis.data;
  if (!d || typeof d !== "object") return null;
  if (d.typ === "huelle-mcp-bereit") return { typ: d.typ };
  if (d.typ === "huelle-dokument-aktiv") return dokumentGueltig(d.dokument) ? { typ: d.typ, dokument: d.dokument } : null;
  if (d.typ === "huelle-abgeben") {
    if (!bereichGueltig(d.bereich)) return null;
    return dokumentGueltig(d.dokument)
      ? { typ: d.typ, bereich: d.bereich, dokument: d.dokument }
      : { typ: d.typ, bereich: d.bereich };
  }
  if (d.typ === "huelle-ki-schritt") {
    if (!bereichGueltig(d.bereich) || typeof d.aktion !== "string" || !AUSLOESER.has(d.ausloeser)) return null;
    const m = { typ: d.typ, bereich: d.bereich, aktion: d.aktion, ausloeser: d.ausloeser };
    if (dokumentGueltig(d.dokument)) m.dokument = d.dokument;
    return m;
  }
  return null;
}

/** Herkunft der Flaeche des iframes (oder null), fuer `nachrichtAusFlaeche`. */
export function flaechenHerkunft(iframe) {
  return herkunftVon(iframe);
}
