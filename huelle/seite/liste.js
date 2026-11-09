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

const nummerVon = (a, i) => (Number.isInteger(a?.nummer) ? a.nummer : i + 1);

/** Erste Nummer nach `ab` mit Status ≠ bestaetigt, am Ende von vorn; null, wenn alle bestätigt sind. */
export function naechsteOffene(aufgaben, ab) {
  if (!Array.isArray(aufgaben) || aufgaben.length === 0) return null;
  const n = aufgaben.length;
  const start = aufgaben.findIndex((a, i) => nummerVon(a, i) === ab);
  for (let schritt = 1; schritt <= n; schritt += 1) {
    const i = (start + schritt + n) % n;
    if (aufgaben[i]?.status !== "bestaetigt") return nummerVon(aufgaben[i], i);
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

/** Die Seite fragt den Stand ab, solange eine Aufgabe bewertet wird. */
export function mussAbfragen(aufgaben) {
  return Array.isArray(aufgaben) && aufgaben.some((a) => a?.status === "in_bewertung");
}
