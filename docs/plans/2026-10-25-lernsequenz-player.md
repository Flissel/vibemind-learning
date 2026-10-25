# Lernsequenz-Player — Plan

Ziel: interaktive Präsentation, interaktives Video mit Haltepunkt-Fenstern, zeichnender Erklär-Rahmen und
Rechenaufgaben mit Rechenweg **jetzt** in LearnHouse-Lektionen, ohne Neubau des LearnHouse-Web-Images
(Docker-VM zu klein). Learning liefert den Player selbst aus; die LearnHouse-Seite bettet ihn über den
vorhandenen Baustein `blockEmbed` (iframe, beliebige URL, keine frame-src-Sperre) ein.

Konzept: Vibemind_V1 `docs/superpowers/specs/2026-10-25-learning-kurssystem-consulting-design.md` (4.1–4.4).

## Feste Regeln

- Lösungen verlassen den Server nie: Der Player bekommt eine Ansicht ohne Lösungen, bewertet wird per POST.
- Jede Folie und jeder Haltepunkt trägt Belege (Quellen-IDs des Kurses).
- Keine DB-Migration in v1: Sequenzen liegen als JSON im Artefakt-Verzeichnis (`lernsequenzen/<id>.json`).
- Zugriff: Der Einbettungs-Link trägt ein HMAC-Token je Sequenz (Schlüssel aus `LEARNING_VIBEMIND_SERVICE_KEY`
  abgeleitet); ohne gültiges Token 404. Antworten zählen in v1 für den lokalen Lernenden (`local-learner`);
  die Zuordnung zur LearnHouse-Person kommt mit Stufe 0b.

## Aufgaben

1. `contracts/lernsequenz.py`: Modelle `LernsequenzV1` (Abschnitte: Folie, Video; Haltepunkte: Erklärung,
   Auswahl, Rechenweg, Schätzen, Zeichen-Erklärung) + öffentliche Ansicht ohne Lösungen. Tests.
2. `services/lernsequenz/bewertung.py`: Auswahl (einfach/mehrfach), Rechenweg mit Toleranz und
   Folgefehlern, Schätzen mit Bandbreite. Tests.
3. `services/lernsequenz/store.py`: speichern/laden im Artefakt-Verzeichnis, sichere Pfade. Tests.
4. `services/lernsequenz/aus_kurs.py`: Präsentation aus einem Kurs bauen (Lektionen → Folien,
   Auswahl-Aufgaben der Engine → Auswahl-Haltepunkte mit Lösungen aus den Engine-Items). Tests.
5. `deployment/lernsequenz_routes.py`: `GET /lernsequenz/{id}` (Player-Seite), `GET .../daten`,
   `POST .../antwort`. Sicherheits-Header, Token. Tests.
6. Player-Seite (eine HTML-Datei, kein Build): Folien, Video mit Haltepunkten, Overlay-Fenster,
   zeichnender Erklär-Rahmen (SVG-Strichanimation, selbst einzeichnen), Rechenweg, Schätzen. Selbst per
   Playwright geprüft.
7. Live: Sequenz für einen veröffentlichten Kurs erzeugen, in die erste Lektion einbetten (Sicherung
   vorher), im LearnHouse-Browser geprüft.
