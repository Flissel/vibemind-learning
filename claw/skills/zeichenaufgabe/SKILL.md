---
name: zeichenaufgabe
description: "Zeichenaufgabe in einer Übungssitzung: Zeichenfenster-Link öffnen, Hinweise zu markierten Objekten, Bewertung erklären und auf menschliche Prüfung hinweisen. Bei Aufgaben der Art 'zeichnen'."
---

# Zeichenaufgabe

Du zeichnest, speicherst und gibst nichts für die Person ab. Das macht sie im Zeichenfenster.
Die Werkzeuge liefern `zustand`, `ergebnis` und `beleg`; Link, Revision und Bewertung stehen in `ergebnis`.

1. **Voraussetzung.** Du hast eine `session_id` (aus `sitzung_starten`) und die `activity_id` der
   Zeichenaufgabe aus `naechste_aufgabe`. Ohne beides zuerst den Skill `ueben` nutzen.
2. **Öffnen.** `zeichenaufgabe_oeffnen(session_id, activity_id, session_ref)`. `session_ref` ist die
   Sitzungsreferenz der Übungssitzung. Gib den zurückgelieferten Link weiter und sage der Person, dass
   sie dort zeichnet und selbst abgibt. Hast du keine `session_ref`, sag das, statt eine zu erfinden.
3. **Hinweis.** Will die Person Hilfe, `zeichenhinweis(session_id, activity_id, canvas_revision,
   session_ref, objekt_ids)` mit der `canvas_revision` aus dem Ergebnis von Schritt 2 (oder dem letzten
   Hinweis) und den Objekt-IDs, die die Person markiert hat. Keine Objekte raten.
4. **Bewertung.** Nach der Abgabe durch die Person: `bewertung_zeigen(evaluation_id, session_id,
   session_ref)`. Erkläre die Bewertung mit den Gründen aus dem Ergebnis. Steht dort, dass die Bewertung
   zurückgestellt ist oder ein Mensch prüft, sag genau das („wird von einem Menschen geprüft“) und gib
   keine eigene Note.
5. Ist das Zeichenfenster oder die Bewertung nicht verfügbar, gib den `fehler` wieder und biete an, mit
   der nächsten Aufgabe weiterzumachen.
