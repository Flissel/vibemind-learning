// Seitenseite des Speicherns: die Huelle bittet die eingebettete Flaeche, PenEchos eigenen Knopf
// „Speichern“ zu druecken. Gegenstueck: `huelle/flaeche/speichern.js`, vom Durchreicher in PenEchos
// Seite eingefuegt. Knoepfe und Abgabe-Haken rufen nur `flaecheSpeichern(iframe)`.

export const ZEITLIMIT_MS = 15000; // laenger als das Zeitlimit der Flaeche (10 s)

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
