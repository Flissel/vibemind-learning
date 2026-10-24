---
name: zeichenaufgabe
description: "Zeichenaufgabe in einer Übungssitzung: Zeichenfläche im Browser öffnen, Hinweise zu markierten Strichen, Bewertung erklären und auf menschliche Prüfung hinweisen. Bei Aufgaben der Art 'zeichnen' (penecho_canvas)."
---

# Zeichenaufgabe

Du zeichnest, speicherst und gibst nichts für die Person ab. Das macht sie auf der Zeichenfläche
im Browser dieses Rechners, mit dem Knopf „Abgeben“; dort sieht sie auch gleich ihre Bewertung.
Die Werkzeuge liefern `zustand`, `ergebnis` und `beleg`.

1. **Voraussetzung.** Du hast eine `session_id` (aus `sitzung_starten`) und die `activity_id` der
   Zeichenaufgabe (die `id` der Aufgabe aus `naechste_aufgabe`). Ohne beides zuerst den Skill `ueben` nutzen.
2. **Öffnen.** `zeichenaufgabe_oeffnen(session_id, activity_id)`. Sag der Person, dass sich die
   Zeichenfläche im Browser geöffnet hat (`zeichenflaeche`), dass sie dort zeichnet und selbst abgibt.
   Einen Link bekommst du nicht; erfinde keinen.
3. **Hinweis.** Will die Person Hilfe, `zeichenhinweis(session_id, activity_id, canvas_revision,
   objekt_ids)` mit der aktuellen `canvas_revision` und den Strich-IDs, die die Person nennt
   (z. B. `strich-3`). Keine Objekte raten.
4. **Bewertung.** Nach der Abgabe durch die Person: `naechste_aufgabe(session_id)` oder
   `fortschritt(session_id)`; nennt die Person eine Bewertung, `bewertung_zeigen(evaluation_id,
   session_id)`. Erkläre die Bewertung mit den Gründen aus dem Ergebnis. Steht dort, dass die Bewertung
   zurückgestellt ist oder ein Mensch prüft, sag genau das („wird von einem Menschen geprüft“) und gib
   keine eigene Note.
5. Ist die Zeichenfläche oder die Bewertung nicht verfügbar, gib den `fehler` wieder und biete an, mit
   der nächsten Aufgabe weiterzumachen.
