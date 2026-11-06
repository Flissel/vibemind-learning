# PenEcho-Board speichern und wieder öffnen (Befund, PenEcho 1.4.2)

Stand 06.11.2026. Gelesen in `node_modules/penecho/src/**`, gemessen an einer eigenen
PenEcho-Instanz (eigener Port, eigener Zustandsordner, eigener Chromium). PenEcho selbst
wurde nicht verändert.

## Kurz

- **Ein MCP-Board übersteht Neustarts, solange derselbe Browser es öffnet.** PenEcho
  schreibt jedes Board laufend in die Arbeitsfläche des Browsers. Gemessen:
  - Neustart von PenEcho bei offenem Browser: Board und Text sind wieder da.
  - Neustart von PenEcho und Browser mit demselben Profil, also wie ein PC-Neustart:
    Board und Text sind wieder da.
- **Wiedergefunden wird das Board über den `sessionKey`.** `penecho_start_session` mit
  demselben `sessionKey` bindet das alte Dokument wieder, ohne neue Fläche.
- **Per MCP lässt sich ein Board nicht an einem Ablageort speichern.** PenEcho 1.4.2 hat
  dafür kein Werkzeug. Deshalb zeigt PenEcho bei MCP-Boards „Not saved“.
- **Ein anderer Browser oder ein geleerter Browserspeicher findet das Board nicht.**
  PenEcho meldet dann `STORAGE_UNAVAILABLE`. Das war der Verlust aus Stufe 1.
- **Abhilfe ohne PenEcho-Eingriff:** Die Person drückt in PenEcho auf „Speichern“
  (Knopf oder Strg+S). Danach liegt eine Kopie in PenEchos Server-Ablage, und jeder Browser
  stellt das Board über denselben `sessionKey` wieder her. Gemessen in einem frischen
  Browserprofil: `recovery.restored: true`, der Text war da.
- **Urheberschaft:** Striche der Person und Antworten der PenEcho-KI lassen sich **nicht**
  unterscheiden, beide liegen in denselben Rasterkacheln. Einzelheiten unten.

## Wo PenEcho ein MCP-Board hält

| Ort | Inhalt | übersteht |
|---|---|---|
| Arbeitsfläche im Browser (IndexedDB `penecho-workspace-documents`, je Herkunft und Profil) | ganzes Dokument: Kacheln, Texte, Widgets, Bindungen, Sitzungen | PenEcho-, Hülle- und PC-Neustart im selben Browserprofil |
| Bindung in PenEcho (`%USERPROFILE%\.penecho\mcp\conversations`) | `sessionKey` → `documentId`, `canvasId` | PenEcho-Neustart |
| Bindung im Dokument selbst (`penechoDocument.bindings`) | `sessionKey` → `documentId` | wie das Dokument |
| Ablageort `server` (`PENECHO_STATE_DIR\canvases\shared`) | gespeicherte Kopie | alles, jeder Browser |
| Ablageorte `device` und `cloud` | gespeicherte Kopie im Browser bzw. in der Cloud | nicht genutzt |

- „Not saved“ heißt: Das Dokument hat keinen Ablageort (`locator: null`).
- Der Inhalt liegt trotzdem in der Arbeitsfläche des Browsers.
  - Hintergrund-Boards werden nach jeder MCP-Änderung geschrieben
    (`canvas-documents.js`, `canvasDocumentsPersist`).
  - Beim sichtbaren Board sichert PenEcho alle 3 s und 750 ms nach jeder Änderung einen
    Wiederherstellungsstand (`canvasDocumentsFlushDraft`). Das gilt auch für die
    Handschrift der Person.
- Die `documentId` eines neuen MCP-Boards leitet PenEcho aus Client und `sessionKey` ab.
- Neue Boards legt PenEcho im **zuletzt verbundenen** Browser an (`service.js`,
  `penecho_start_session`). Ist das ein Test- oder Zweitbrowser, liegt das Board nur dort.

## Werkzeuge und ihre Rolle

| Werkzeug | Argumente (schema.js) | Befund |
|---|---|---|
| `penecho_start_session` | `title`, `sessionKey`, `restore`, `documentId`, `canvasId`, `show` | bindet über `sessionKey` oder `documentId` das vorhandene Dokument; sucht es in der Arbeitsfläche, dann in den Ablageorten |
| `penecho_inspect_session` | `sessionId` | liefert u. a. `documentId` und `canvasId` der Sitzung, ohne Bild |
| `penecho_list_canvases` | keine | offene Dokumente je verbundenem Browser (`instanceId`, `canvasId`, `documentId`) |
| `penecho_open_canvas` | `instanceId`, `canvasId`, `requestId`, dazu `documentId` oder `locator` | öffnet ein Dokument in genau diesem Browser, auch ein geschlossenes; speichert nicht |
| `penecho_find_canvases` | `instanceId`, `canvasId`, `documentId` | sucht nur unter den offenen Dokumenten |
| `penecho_rename_canvas` | `instanceId`, `canvasId`, `documentId`, `title`, `requestId` | benennt nur eine schon gespeicherte Kopie um (`saved: false` bei MCP-Boards) |
| `penecho_close_session` | `sessionId` | schließt die Sitzung, das Dokument bleibt |
| Prompt `penecho_resume_document` | `documentId` | nur Anweisungstext für ein KI-Modell, kein Speichern |

Gespeichert wird in PenEcho nur über die Oberfläche: `saveCurrentCanvas` in
`persistence.js`, ausgelöst durch den Knopf `#saveCanvasBtn` oder Strg+S. Danach markiert
PenEcho spätere MCP-Änderungen als „Edited“. Die Server-Kopie bleibt auf dem Stand des
letzten Klicks; die Arbeitsfläche im Browser ist weiter aktuell.

## Gemessen (eigene Instanz, Port 3899, MCP über den eigenen direkten HTTP-Zugang)

| Fall | Ergebnis |
|---|---|
| A: Board per MCP anlegen, Text „Aufgabe 1: Probe-Text 4711“ | in IndexedDB, `locator: null` |
| B: PenEcho neu, Browser offen | alte `sessionId`: `session_not_found`; `penecho_start_session` mit demselben `sessionKey`: dasselbe Dokument, Text da, kein Ersatzschlüssel |
| C: Browser zu, PenEcho neu, Browser neu (gleiches Profil) | wie B |
| D: frisches Browserprofil | `penecho_start_session` und `penecho_open_canvas`: `STORAGE_UNAVAILABLE` |
| E: Klick auf „Speichern“, dann frisches Profil | Datei in `canvases/shared`, Anzeige „Saved“; im frischen Profil `recovery.restored: true`, Text da |
| Zwei Browser, der zweite verbindet sich später | Board bleibt im ersten, `sitzung()` findet es |
| Nur noch der fremde Browser | `sitzung()` fragt jeden Browser per `penecho_open_canvas`, findet nichts, legt eine Ersatzfläche an |

Eine Probefläche lässt sich per MCP nicht löschen, es gibt kein Löschwerkzeug. In der
Probe wurde sie mit dem Temp-Ordner der eigenen Instanz entfernt.

## Was die Hülle daraus macht (`penecho_mcp.py`)

- `sitzung()` wie bisher über den `sessionKey`. Die `documentId` aus jeder Antwort steht in
  `dokumente.json` neben `flaechen.json`.
- Meldet PenEcho die Fläche als verloren und ist eine `documentId` gemerkt, fragt die Hülle
  jeden verbundenen Browser (`penecho_list_canvases`, dann `penecho_open_canvas`). Öffnet
  einer das Dokument, bindet `penecho_start_session` mit dieser `documentId` die Sitzung
  daran. Erst wenn kein Browser das Dokument hat, entsteht wie bisher eine Ersatzfläche.
- `speichern(session_id)` ruft `penecho_inspect_session` und merkt die `documentId`.
  Gespeichert wird dabei nichts, denn PenEcho sichert die Arbeitsfläche selbst. Eine
  Server-Kopie entsteht nur über PenEchos eigenen Knopf „Speichern“.

## Urheberschaft im Dokument (für den Bericht)

**Nein, Handschrift der Person und Antworten der PenEcho-KI sind nicht unterscheidbar.**

- Antworten der Stift-KI, z. B. „answer“, „continue“ oder „solve“, werden beim Übernehmen
  in die Rasterkacheln gemalt (`ai-runtime.js`, `acceptPending`: `blitSized`/`blitClipped`).
  Dort liegen auch die Striche der Person. Im gespeicherten Dokument sind das nur PNG-Kacheln
  (`format: penecho-raster-tiles`), ohne Herkunft.
- Textfelder haben nur `id`, Lage, Schrift, Farbe und `text`, kein Herkunftsfeld.
- Widgets haben `pluginId` `general`, gleich ob von der Hülle, vom PenEcho-Agenten oder aus
  den Vorschlägen.
- Unterscheidbar sind nur Teilmengen:
  - Funktionsplots der KI sind Bilder mit `plotExpression`.
  - Widgets der Hülle stehen in `penechoWorkspace.sessions[].artifacts` (Client
    „External AI“, `sessionKey` der Hülle) mit ihrer `objectId`.
  - Widgets des PenEcho-Agenten stehen in `penechoWorkspace.internalSessions[].artifacts`
    (Client „PenEcho Agent“).
  - `penechoWorkspace.changes` protokolliert Art und `objectId` einer Änderung, aber nicht,
    wer sie gemacht hat.

Folge nach Festlegung 6 der Spec: Der Bericht zeigt je Aufgabe einen Schnappschuss des
Aufgabenbereichs, keine ausgeschnittenen KI-Erklärungen.
