import { FEHLER_TEXTE, meldungFuer, NICHT_LESBAR } from "./aufgabe.js";

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

// ---- Ein Canvas je Aufgabe: Dokumentzuordnung und „Kurs abgeben“ ----------------------------------------------

/** Rückfrage vor „Kurs abgeben“ (global.md). */
export const KURS_ABGEBEN_RUECKFRAGE =
  "Alle offenen Aufgaben abgeben und den Kurs abschließen? Nicht bearbeitete Aufgaben zählen 0 Punkte.";

/** Aufgabennummer zu einer PenEcho-Dokument-ID (`dokumente`: {documentId: nummer}); unbekannt: null. */
export function nummerAusDokument(dokumente, dokument) {
  if (!dokumente || typeof dokumente !== "object" || typeof dokument !== "string") return null;
  if (!Object.hasOwn(dokumente, dokument)) return null;
  const nummer = dokumente[dokument];
  return Number.isInteger(nummer) ? nummer : null;
}

/**
 * Text zum Stand von „Kurs abgeben“. Bei einem Fehler ist `meldung` ein bekannter Fehlercode (fester Text)
 * oder ein fertiger deutscher Text der Brücke (unverändert).
 */
export function kursAbgabeText(kursabgabe) {
  if (!kursabgabe || typeof kursabgabe !== "object") return "";
  if (kursabgabe.zustand === "laeuft") return `Aufgabe ${kursabgabe.aktuell} von ${kursabgabe.gesamt} wird bewertet …`;
  if (kursabgabe.zustand === "fertig") return "Kurs abgegeben.";
  if (kursabgabe.zustand === "fehler") {
    const m = typeof kursabgabe.meldung === "string" ? kursabgabe.meldung : "";
    return Object.hasOwn(FEHLER_TEXTE, m) ? meldungFuer({ fehler: m }) : m || meldungFuer({});
  }
  return "";
}

/** Der Knopf „Kurs abgeben“: nur im Modell „ein Canvas je Aufgabe“, nicht am Kursende, nicht während eines Laufs. */
export function kursAbgebenSichtbar({ jeAufgabe, phase, kursabgabe }) {
  return jeAufgabe === true && phase !== "ende" && kursabgabe?.zustand !== "laeuft";
}

/** Ein nicht gesendeter Antwortentwurf (Typ `code`) sperrt „Kurs abgeben“, solange die Aufgabe nicht bestätigt ist. */
export function entwurfSperrt({ typ, status, entwurf }) {
  return typ === "code" && status !== "bestaetigt" && typeof entwurf === "string" && entwurf.trim() !== "";
}

export const entwurfText = (nummer) =>
  `Bitte senden Sie zuerst Ihre Antwort zu Aufgabe ${nummer} ab, bevor Sie den Kurs abgeben.`;
