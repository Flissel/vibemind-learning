// Seitenseite des Speicherns: die Huelle bittet die eingebettete Flaeche, PenEchos eigenen Knopf
// „Speichern“ zu druecken. Gegenstueck: `huelle/flaeche/speichern.js`, vom Durchreicher in PenEchos
// Seite eingefuegt. Knoepfe und Abgabe-Haken rufen nur `flaecheSpeichern(iframe)`.

export const ZEITLIMIT_MS = 15000; // laenger als das Zeitlimit der Flaeche (10 s)

// Teil B, eigene Flaeche je Person: jeder Tab hat eine Flaechen-Kennung lf-<32 hex>. Sie geht als `&flaeche=` in die
// Adresse der Flaeche (die meldet sich damit als canvasId beim MCP) und als Kopf X-Lernflaeche an jede API-Anfrage.
// sessionStorage gilt je Tab und ueberlebt das Neuladen; so bleibt die Flaeche eines Tabs dieselbe.
export const KENNUNG_SCHLUESSEL = "lernflaeche-kennung";
const KENNUNG_MUSTER = /^lf-[0-9a-f]{32}$/;

export function flaechenKennung({ speicher, zufall = (feld) => globalThis.crypto.getRandomValues(feld) } = {}) {
  try {
    // Schon der Zugriff auf sessionStorage wirft, wenn der Browser Speicher sperrt.
    if (speicher === undefined) speicher = globalThis.sessionStorage;
    const alt = speicher?.getItem(KENNUNG_SCHLUESSEL);
    if (typeof alt === "string" && KENNUNG_MUSTER.test(alt)) return alt;
  } catch {
    // gesperrter Speicher: dann gilt die Kennung nur bis zum Neuladen
  }
  const feld = zufall(new Uint8Array(16));
  const neu = "lf-" + Array.from(feld, (b) => b.toString(16).padStart(2, "0")).join("");
  try {
    speicher?.setItem(KENNUNG_SCHLUESSEL, neu);
  } catch {
    // wie oben
  }
  return neu;
}

export function flaechenAdresse(basis, kennung) {
  if (typeof kennung !== "string" || !KENNUNG_MUSTER.test(kennung)) return basis;
  try {
    const url = new URL(basis);
    url.searchParams.set("flaeche", kennung);
    return url.toString();
  } catch {
    return basis;
  }
}

export function flaechenKopf(kennung) {
  return typeof kennung === "string" && KENNUNG_MUSTER.test(kennung) ? { "X-Lernflaeche": kennung } : {};
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
