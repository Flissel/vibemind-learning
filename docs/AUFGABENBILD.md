# Bild an der Aufgabe

Offene Aufgaben, Fälle und Skizzen können ein Bild tragen, das Lernende benennen oder beschreiben. Das Bild
kommt aus Wikimedia Commons und wird als Kopie abgelegt, zusammen mit Titel, Urheber und Lizenz.

## Ablauf

| Schritt | Wer | Was geschieht |
|---|---|---|
| Suchen | Learning-Claw (`werkstatt_bild_suchen`) | Suche bei Commons; zurück kommen nur Bilder mit freier Lizenz, je mit Dateiseite. |
| Wählen | Inhaberin | Sie sieht sich die Dateiseiten an und nennt das Bild. Der Claw wählt nie selbst. |
| Ablegen | Learning-Claw (`werkstatt_bild_setzen`) | Fragt Commons erneut nach genau dieser Datei, prüft die Lizenz, holt eine verkleinerte Fassung, legt sie im Medienordner ab und trägt `bild` als neue Fassung ein. |
| Freigeben | Inhaberin | Das Bild erscheint in der Vorschau; für Lernende gilt es erst nach der Freigabe. Ein neues Bild ist eine Textkorrektur, der Lernstand bleibt. |
| Zeigen | Hülle | Das Bild steht auf der Fläche neben der Aufgabe, die Karte nennt die Quelle und verlinkt die Dateiseite. |

## Lizenzregel

Erlaubt sind gemeinfrei („Public domain“), CC0, CC BY und CC BY-SA in den Versionen 1.0 bis 4.x
(`services/werkstatt/aufgabenbild.py`, `lizenz_erlaubt`). Alles andere wird abgelehnt: nicht gewerblich (NC),
keine Bearbeitung (ND), GFDL, unbekannte oder leere Angaben. Verlangt die Lizenz die Nennung des Urhebers und
Commons nennt keinen, wird das Bild ebenfalls abgelehnt.

Die Regel steht an zwei Stellen: im Werkzeug vor dem Ablegen und im Schema des Inhalts. Ein Bild mit
unzulässiger Lizenz lässt sich deshalb gar nicht speichern. Titel, Urheber und Lizenz kommen immer aus den
Angaben von Commons; `werkstatt_aufgabe_aendern` nimmt das Feld `bild` nicht an.

Die Regel ist eine technische Vorauswahl. Die Angaben bei Commons können falsch sein, und Rechtsberatung ist
das nicht.

## Technik

- **Kontaktangabe:** Wikimedia verlangt Namen und Kontakt eines automatischen Abrufers. Die Angabe steht in
  `LEARNING_COMMONS_KONTAKT` (Webadresse oder E-Mail-Adresse des Betreibers). Ohne sie fragt der Claw Commons
  nicht.
- **Datei:** `/lernsequenz-medien/bild-<16 Hex-Zeichen des SHA-256>.png|jpg`. Verkleinert auf 1000 px Breite,
  bei mehr als 580.000 Bytes auf 700 und dann 500 px. SVG liefert Commons als PNG. Abgerufen wird nur von den
  Bildrechnern von Wikimedia (`upload.wikimedia.org`, `thumb.wikimedia.org`).
- **Suche:** auf Bilder und Zeichnungen beschränkt (`filetype:bitmap|drawing`); ohne den Zusatz liefert Commons
  zu vielen Suchwörtern vor allem PDF-Dateien und Videos.
- **Skizzen brauchen eine Quelle:** Eine Aufgabe vom Typ `sketch` lässt sich nur freigeben, wenn sie eine Quelle
  des Kurses zitiert (`skizze_ohne_quelle`). Offene Aufgaben mit Bild gehen auch ohne.
- **Inhalt:** `bild` an `open`, `case` und `sketch` mit `image`, `width`, `height`, `titel`, `urheber`,
  `lizenz`, `lizenz_url`, `quelle_url`.
- **Katalog und Engine:** `presentation.image`, `width`, `height` und `bildquelle`; die Engine lässt diese
  Felder für `open`, `case` und `penecho_canvas` durch.
- **Bilder werden nicht bearbeitet.** Eingetragene Namen bleiben im Bild; für „Benennen“ braucht es eine Fassung
  ohne Namen.

## Diagramm statt Bild

Für Abläufe und Aufbau zeichnet der Learning-Claw selbst (`werkstatt_diagramm_setzen`). Das Diagramm steht an
derselben Stelle wie ein Bild von Commons und ersetzt es; weggenommen wird es mit `werkstatt_bild_setzen(…, None)`.

| Schritt | Was geschieht |
|---|---|
| Liste | Der Claw übergibt einen Titel, bis zu 30 Kästen, bis zu 60 Pfeile, die Richtung und optional Gruppen. |
| Text für Graphviz | `services/werkstatt/diagramm.py` baut daraus den Graphviz-Text. Kennungen, Attribute und Aussehen stehen im Code; Text aus dem Chat erscheint nur als Beschriftung in Anführungszeichen. |
| Zeichnen | `claw/zeichner.py` ruft Graphviz (`dot`) auf dem Rechner des Claw auf, höchstens 10 Sekunden, Text über die Eingabe. |
| Ablegen | Das PNG (höchstens 1000 px breit und 580.000 Bytes) geht in den Medienordner, die Liste steht als `bild.diagramm` an der Fassung. |
| HTML-Seite | Dieselbe Zeichnung als SVG in einer einzelnen HTML-Datei ohne Skript und ohne nachgeladene Dateien, im Ordner `diagramme` neben `learning.env` (oder `LEARNING_DIAGRAMM_ORDNER`). Das Werkzeug nennt den Pfad. |

- **Leere Kästen:** Ein Kasten mit `"leer": true` zeigt eine Nummer statt seines Textes. Der Text bleibt in der
  Liste an der Fassung und geht nicht zu den Lernenden; im Katalog steht nur Bild, Titel und `eigen: true`.
- **Warum kein freier Graphviz-Text:** Graphviz kann über Attribute lokale Dateien in ein Bild einbinden. Der Claw
  liefert deshalb nur die Liste.
- **Quellenzeile:** „Diagramm: <Titel>, eigene Darstellung“, ohne Link und ohne Lizenz.
- **Niemand prüft den Inhalt.** Ob Reihenfolge und Beschriftung fachlich stimmen, sieht nur die Inhaberin in der
  Vorschau.
- **Voraussetzung:** Graphviz ist auf dem Rechner des Claw installiert; ohne meldet das Werkzeug das.

## KI-erzeugte Szene

Für Szenen ohne einzig richtige Antwort (beschreiben, beurteilen) zeichnet die Bild-KI
(`werkstatt_szene_setzen`), nur an offenen Aufgaben und Fällen. Für Fachliches ist sie nicht gedacht: Sie
zeichnet Aufbau, Abläufe und Beschriftungen falsch.

- **Wo gezeichnet wird:** im ComfyUI, das auf dem Rechner des Claw schon läuft, mit FLUX.2 klein
  (`claw/szene_flux2.json`). Angesprochen wird nur dieser Rechner (`LEARNING_COMFYUI_URL`, Vorgabe
  `http://127.0.0.1:8188`); die Beschreibung einer Aufgabe verlässt ihn nicht. ComfyUI wird nur benutzt, nie
  gestartet oder beendet, und seine Modelle bleiben geladen.
- **Ein Bild nach dem anderen:** Ist die Warteschlange von ComfyUI nicht leer, stellt das Werkzeug keinen
  Auftrag und meldet „besetzt“. Zwischen Nachsehen und Auftrag liegt ein Augenblick; stellt genau dann jemand
  anderes ein Bild ein, warten beide hintereinander.
- **Fester Rahmen:** An jede Beschreibung hängt der Code an: kein Text, keine Logos oder Marken, keine echten
  Personen. Das Modell hält sich daran meistens, nicht immer.
- **Frist:** sieben Minuten. Bleibt die Antwort auf den Auftrag aus, wird er nicht neu gestellt, sondern gesucht.
- **Ablage:** als JPEG (höchstens 580.000 Bytes) im Medienordner; Beschreibung und Modell stehen als
  `bild.beschreibung` und `bild.modell` an der Fassung. ComfyUI behält zusätzlich seine eigene Kopie im
  Ausgabeordner (`lernaufgabe-szene_…`).
- **Quellenzeile:** „Bild: KI-erzeugt“; im Katalog steht nur `bildquelle: {ki: true}`.
- **Hinweis in der Freigabe:** Jede Aufgabe mit KI-Bild erzeugt den Befund `bild_ki`. Er sperrt nicht.

## Offen

- Ob die Bewertung zuverlässig erkennt, dass ein handgeschriebener Name am richtigen Teil steht, ist nicht
  gemessen.
- Die Quellenangabe steht auf der Karte, nicht im PDF-Bericht.
- Die HTML-Seite eines Diagramms gelangt nicht von selbst nach Rowboat; die Inhaberin übernimmt die Datei.
- Andere Diagrammarten (Zeitachsen, Sequenzen, Kurven) sind eine eigene Stufe.
- Die Bild-KI teilt sich die Grafikkarte mit anderen Diensten; eine gemeinsame Warteschlange gibt es nicht.
