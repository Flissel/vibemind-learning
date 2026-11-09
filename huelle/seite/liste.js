import { NICHT_LESBAR } from "./aufgabe.js";

// Listenkurs: reine Hilfen für die Kopfzeile „‹ Aufgabe k von n ›“ und die Abgaben im Hintergrund.
// Der Stand kommt von der Brücke (`GET …/stand`): je Aufgabe {nummer, status, urteil, vorbewertung_id, meldung}.

const STATUS_TEXT = {
  offen: "offen",
  in_bewertung: "wird bewertet …",
  rueckmeldung: "Rückmeldung da",
  bestaetigt: "erledigt",
};

// Neue feste Meldungen der Brücke im Listenkurs (409 mit code laeuft_schon / schon_bestaetigt).
export const LISTEN_MELDUNGEN = new Set([
  "Diese Aufgabe wird gerade bewertet.",
  "Diese Aufgabe ist schon bestätigt.",
]);

/**
 * Ergebnis der gewählten Aufgabe für die Leiste: nur mit Rückmeldung, die nicht „überarbeitet“ wurde.
 * Hat die Brücke die Karte nicht auf die Fläche gebracht, trägt der Eintrag `karte` (Text für den Hinweis).
 */
export function listenErgebnisAus(a, verworfen) {
  if (!a || a.status !== "rueckmeldung" || typeof a.vorbewertung_id !== "string") return null;
  if (verworfen?.has(a.vorbewertung_id)) return null;
  const urteil = typeof a.urteil === "string" ? a.urteil : "";
  const ergebnis = { vorbewertung_id: a.vorbewertung_id, urteil, lesbar: urteil !== NICHT_LESBAR };
  if (typeof a.karte === "string" && a.karte) ergebnis.karte = a.karte;
  return ergebnis;
}

/** Der Link „Zusammenfassung (PDF)“ gilt jedem Listenkurs, auch einem beendeten, den die Seite neu öffnet. */
export function zusammenfassungSessionId(liste, listeEnde, turn) {
  if (liste === null && !listeEnde) return null;
  const sid = turn?.session_id;
  return typeof sid === "string" && sid !== "" ? sid : null;
}

const nummerVon = (a, i) => (Number.isInteger(a?.nummer) ? a.nummer : i + 1);

/**
 * Erste Nummer nach `ab`, am Ende von vorn, die weder bestätigt ist noch gerade bewertet wird; gibt es keine,
 * die erste nicht bestätigte (Abnahme A2, Befund 3). null, wenn alle bestätigt sind. Wie bruecke._naechste_offene.
 */
export function naechsteOffene(aufgaben, ab) {
  if (!Array.isArray(aufgaben) || aufgaben.length === 0) return null;
  const n = aufgaben.length;
  const start = aufgaben.findIndex((a, i) => nummerVon(a, i) === ab);
  for (const ueberspringen of [["bestaetigt", "in_bewertung"], ["bestaetigt"]]) {
    for (let schritt = 1; schritt <= n; schritt += 1) {
      const i = (start + schritt + n) % n;
      if (!ueberspringen.includes(aufgaben[i]?.status)) return nummerVon(aufgaben[i], i);
    }
  }
  return null;
}

/** Kurztext zum Status einer Aufgabe; `fehler` liefert die Meldung der Brücke. */
export function statusText(status, meldung = "") {
  if (status === "fehler") return typeof meldung === "string" ? meldung : "";
  return Object.hasOwn(STATUS_TEXT, status) ? STATUS_TEXT[status] : "";
}

/** „1 Abgabe wird bewertet“ / „n Abgaben werden bewertet“; leer ohne wartende Abgabe. */
export function warteText(wartend) {
  if (!Number.isInteger(wartend) || wartend <= 0) return "";
  return wartend === 1 ? "1 Abgabe wird bewertet" : `${wartend} Abgaben werden bewertet`;
}

/**
 * Ob und wie ein Stand der Brücke übernommen wird; null heißt verwerfen.
 * - `anfrage`: laufende Nummer der Anfrage, die ihn brachte (die eigene Antwort einer Abgabe: Infinity).
 * - `gueltigAb`: Nummer zur letzten Abgabe (gesetzt bei deren Beginn und bei deren Antwort). Eine
 *   Anfrage, die davor begann, kann die Abgabe noch nicht kennen: veraltet.
 * - `sendend`: Nummern, deren Abgabe noch unterwegs ist; sie bleiben „in_bewertung“ (außer bestätigt).
 * Liefert {aktuell, wartend, aufgaben} als neue Liste; die Eingabe bleibt unverändert.
 */
export function standAnnehmen(stand, { anfrage, gueltigAb, sendend }) {
  if (stand?.ablauf !== "liste" || !Array.isArray(stand.aufgaben) || stand.aufgaben.length === 0) return null;
  if (anfrage < gueltigAb) return null;
  const aufgaben = stand.aufgaben.map((a) => (sendend?.has(a?.nummer) && a.status !== "bestaetigt"
    ? { ...a, status: "in_bewertung", meldung: null }
    : a));
  return { aktuell: stand.aktuell, wartend: stand.wartend, aufgaben };
}

/** Neue Liste, in der der Eintrag mit dieser Nummer `aenderung` trägt; ohne diese Nummer unverändert. */
export function eintragSetzen(liste, nummer, aenderung) {
  if (!liste || !Array.isArray(liste.aufgaben) || !liste.aufgaben.some((a) => a?.nummer === nummer)) return liste;
  return { ...liste, aufgaben: liste.aufgaben.map((a) => (a?.nummer === nummer ? { ...a, ...aenderung } : a)) };
}

/** Die Seite fragt den Stand ab, solange eine Aufgabe bewertet wird. */
export function mussAbfragen(aufgaben) {
  // Auch solange die Brücke eine Karte noch auf die Fläche setzt: scheitert sie, kommt ihr Text als `karte`.
  return Array.isArray(aufgaben) && aufgaben.some((a) => a?.status === "in_bewertung" || a?.karte_ausstehend === true);
}
