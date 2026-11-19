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
  bei mehr als 580.000 Bytes auf 700 und dann 500 px. SVG liefert Commons als PNG. Abgerufen wird nur von
  `upload.wikimedia.org`.
- **Inhalt:** `bild` an `open`, `case` und `sketch` mit `image`, `width`, `height`, `titel`, `urheber`,
  `lizenz`, `lizenz_url`, `quelle_url`.
- **Katalog und Engine:** `presentation.image`, `width`, `height` und `bildquelle`; die Engine lässt diese
  Felder für `open`, `case` und `penecho_canvas` durch.
- **Bilder werden nicht bearbeitet.** Eingetragene Namen bleiben im Bild; für „Benennen“ braucht es eine Fassung
  ohne Namen.

## Offen

- Ob die Bewertung zuverlässig erkennt, dass ein handgeschriebener Name am richtigen Teil steht, ist nicht
  gemessen.
- Die Quellenangabe steht auf der Karte, nicht im PDF-Bericht.
- Gerenderte Diagramme (Mermaid) und Bild-KI für Beschreiben-Aufgaben sind eigene Stufen.
