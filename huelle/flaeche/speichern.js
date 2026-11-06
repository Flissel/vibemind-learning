// Laeuft in PenEchos eigener Seite: der Durchreicher fuegt es als Modul ein (`/__huelle/speichern.js`).
// Die Huelle-Seite schickt `{typ:"huelle-speichern", id}`; dieses Skript drueckt PenEchos eigenen
// Knopf „Speichern“ und antwortet `{typ:"huelle-gespeichert", id, ok, grund}`. PenEcho bleibt
// unveraendert. Gelesen in PenEcho 1.4.2: public/index.html (#saveCanvasBtn,
// #canvasDocumentSaveState) und app.js (saveCurrentCanvas, updateStudioDocumentState:
// data-state = unsaved | saving | saved | edited).

export const ANFRAGE = "huelle-speichern";
export const ANTWORT = "huelle-gespeichert";
export const ZEITLIMIT_MS = 10000;
// Maschinenlesbarer `code` jeder Antwort (neben dem Klartext `grund`). Die Seite entscheidet nur nach `code`:
//   gespeichert         PenEcho zeigt „Saved“
//   geaendert           die Flaeche wurde waehrend des Speicherns veraendert (ein Wiederholen lohnt)
//   leer                PenEcho hat nichts gespeichert (leere Flaeche oder Fehler)
//   kein_knopf          PenEchos Knopf oder Anzeige fehlt oder ist gesperrt
//   zeitueberschreitung PenEcho speichert noch nach dem Zeitlimit
//   fehler              alles andere (unbekannter Zustand, Ausnahme, Speichern nicht begonnen)
// Nur auf der Seite entstehen: keine_flaeche (kein Rahmen) und keine_antwort (Zeitlimit ohne Antwort).
const MAX_ID = 128;
const TAKT_MS = 100;

/** Die erlaubten Huelle-Herkuenfte aus dem Meta-Tag des Durchreichers; nur exakte Herkuenfte. */
export function herkuenfteAus(text) {
  const aus = [];
  for (const teil of String(text ?? "").split(/\s+/)) {
    if (!teil) continue;
    try {
      if (new URL(teil).origin === teil) aus.push(teil);
    } catch {
      // kein gueltiger Eintrag
    }
  }
  return aus;
}

/** `{id, origin}` fuer eine gueltige Anfrage des Elternfensters aus einer erlaubten Herkunft, sonst null. */
export function anfrageAus(ereignis, erlaubt, eltern) {
  if (!ereignis || ereignis.source !== eltern || !erlaubt.includes(ereignis.origin)) return null;
  const daten = ereignis.data;
  if (!daten || typeof daten !== "object" || daten.typ !== ANFRAGE) return null;
  if (typeof daten.id !== "string" || !daten.id || daten.id.length > MAX_ID) return null;
  return { id: daten.id, origin: ereignis.origin };
}

/** null, solange PenEcho noch speichert; sonst das Ergebnis. */
export function zustandBewerten(zustand) {
  switch (zustand) {
    case "saving":
      return null;
    case "saved":
      return { ok: true, grund: null, code: "gespeichert" };
    case "unsaved":
      return { ok: false, grund: "PenEcho hat nicht gespeichert (leere Fläche oder Fehler)", code: "leer" };
    case "edited":
      return { ok: false, grund: "PenEcho hat nicht gespeichert (ungespeicherte Änderungen)", code: "geaendert" };
    default:
      return { ok: false, grund: `PenEcho meldet einen unbekannten Zustand: ${String(zustand)}`, code: "fehler" };
  }
}

const warten = (ms) => new Promise((fertig) => setTimeout(fertig, ms));

/** Drueckt PenEchos Knopf „Speichern“ und wartet, bis PenEcho „Saved“ zeigt. Nie still. */
export async function speichernAusloesen(dokument, {
  zeitlimitMs = ZEITLIMIT_MS, schlaf = warten, jetzt = () => Date.now(),
} = {}) {
  const knopf = dokument.querySelector("#saveCanvasBtn");
  if (!knopf) return { ok: false, grund: "PenEcho-Knopf #saveCanvasBtn fehlt", code: "kein_knopf" };
  const anzeige = dokument.querySelector("#canvasDocumentSaveState");
  if (!anzeige || !anzeige.dataset) return { ok: false, grund: "PenEcho-Anzeige #canvasDocumentSaveState fehlt", code: "kein_knopf" };
  // Laeuft schon ein Speichern, wird auf dessen Ergebnis gewartet statt erneut zu klicken.
  if (anzeige.dataset.state !== "saving") {
    if (knopf.disabled) return { ok: false, grund: "PenEcho-Knopf ist gesperrt", code: "kein_knopf" };
    knopf.click();
    // PenEcho setzt „saving“ synchron im Klick (setHistorySaveBusy(true)).
    if (anzeige.dataset.state !== "saving") return { ok: false, grund: "PenEcho hat das Speichern nicht begonnen", code: "fehler" };
  }
  const ende = jetzt() + zeitlimitMs;
  for (;;) {
    const ergebnis = zustandBewerten(anzeige.dataset.state);
    if (ergebnis) return ergebnis;
    if (jetzt() >= ende) return { ok: false, grund: "Zeitlimit: PenEcho speichert noch", code: "zeitueberschreitung" };
    await schlaf(TAKT_MS);
  }
}

/** Hoert auf Anfragen der Huelle-Seite; liefert die Abmeldung. */
export function installieren(fenster, erlaubt, optionen = {}) {
  const hoerer = async (ereignis) => {
    const anfrage = anfrageAus(ereignis, erlaubt, fenster.parent);
    if (!anfrage) return;
    let ergebnis;
    try {
      ergebnis = await speichernAusloesen(fenster.document, optionen);
    } catch (fehler) {
      ergebnis = { ok: false, grund: `Fehler beim Speichern: ${String(fehler?.message ?? fehler)}`, code: "fehler" };
    }
    ereignis.source.postMessage({ typ: ANTWORT, id: anfrage.id, ok: ergebnis.ok, grund: ergebnis.grund, code: ergebnis.code }, anfrage.origin);
  };
  fenster.addEventListener("message", hoerer);
  return () => fenster.removeEventListener("message", hoerer);
}

// Im Browser: nur eingebettet und nur mit erlaubten Herkuenften aus dem Meta-Tag des Durchreichers.
if (typeof window !== "undefined" && typeof document !== "undefined" && window.parent && window.parent !== window) {
  const erlaubt = herkuenfteAus(document.querySelector('meta[name="huelle-herkuenfte"]')?.getAttribute("content"));
  if (erlaubt.length) installieren(window, erlaubt);
}
