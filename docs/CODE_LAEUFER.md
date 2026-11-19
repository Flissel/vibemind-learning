# Code-Läufer: Code von Lernenden lokal ausführen

Stand 15.11.2026. Programmieraufgaben (Typ `code`) können ihren Code jetzt wirklich ausführen. Der Code läuft auf dem
PC, auf dem die Hülle läuft, in einer eigenen abgeschotteten Umgebung.

## Entscheidung

Der Code läuft in einer eigenen WSL2-Distro `vibemind-code` (Alpine Linux mit Python und Node.js). WSL2 ist eine
Hyper-V-VM auf dem Windows-PC. Jeder Lauf bekommt darin eine Wegwerf-Sandbox (bubblewrap).

Gründe:
- WSL2 läuft auf dem PC ohnehin; ein Lauf kostet etwa 0,1 s und kaum Speicher.
- Ein eigener Windows-Gast (Windows Sandbox, Hyper-V-VM) braucht Adminrechte, einen Neustart und mindestens 4 GB RAM
  je Lauf. Beim Aufbau waren auf dem PC 3,3 GB frei.
- Die Kurse brauchen Python und JavaScript, kein Windows.

Nicht gewählt:
- Direkt auf dem PC ausführen: fremder Code hätte Zugriff auf Dateien, Dienste und Schlüssel.
- Im Browser (Pyodide): keine verlässlichen Grenzen, anderes Verhalten als echtes Python.
- Auf dem Server mit Datenbank und Diensten: fremder Code gehört nicht neben Produktionsdaten.

## Abschottung

Zwei Schichten.

Die Distro selbst (`/etc/wsl.conf`):
- Windows-Laufwerke werden nicht eingehängt (`automount` aus), es gibt kein `/mnt/c`.
- Windows-Programme lassen sich nicht starten (`interop` aus).
- Der Benutzer `laeufer` hat eine eigene Benutzer-ID (61234) und keine besonderen Rechte.

Jeder Lauf (`lauf.py`, bubblewrap):
- kein Netz. WSL teilt im Modus `mirrored` das Netz des PCs; ohne diese Sperre wären die Dienste auf `127.0.0.1` des
  PCs erreichbar;
- sichtbar sind nur `/usr`, `/lib`, `/bin` (lesend), dazu `/work` und `/tmp` als tmpfs mit je 16 MB;
- leere Umgebung, eigene Prozesswelt, keine verschachtelten Namensräume;
- Grenzen: Laufzeit 5 s (höchstens 20 s), 512 MB Speicher, 8 MB je Datei, 64 Prozesse, 20.000 Zeichen Ausgabe je
  Kanal, höchstens zwei Läufe gleichzeitig.

Der Quelltext geht als JSON über die Standardeingabe in die Distro, nie über die Kommandozeile.

## Restrisiko

- Alle WSL-Distros teilen sich einen Linux-Kernel. Ein Fehler im Kernel könnte die Sandbox durchbrechen und andere
  Distros auf dem PC erreichen. Für Kurse mit eingeladenen Personen ist das vertretbar; für offene Kurse mit
  Unbekannten wäre eine eigene VM je Lauf die nächste Stufe.
- Die Speichergrenze gilt je Prozess. Zusammen mit der Prozessgrenze sind theoretisch mehrere GB möglich, begrenzt
  durch den Speicher der WSL-VM.

## Einrichten

```powershell
pwsh -File huelle/laeufer/einrichten.ps1        # anlegen oder nachziehen
pwsh -File huelle/laeufer/einrichten.ps1 -Neu   # verwerfen und frisch aufbauen
```

Das Skript braucht keine Adminrechte, ändert `.wslconfig` nicht und fährt WSL nicht herunter. Es lädt das
Alpine-Grundsystem, prüft dessen Prüfsumme, spielt `lauf.py` ein und endet mit einer Probe der Abschottung.
Nach einer Änderung an `lauf.py` das Skript erneut ausführen.

Entfernen: `wsl --unregister vibemind-code`.

## Schnittstellen

- Python: `spaces.learning.huelle.laeufer.ausfuehren(sprache, quelltext, eingabe="", zeit_s=5) -> Lauf`.
  Fehler im Code und Zeitüberschreitung sind Ergebnisse. `UngueltigerAuftrag` und `LaeuferNichtBereit` sind Ausnahmen.
- Hülle: `POST /lernen/api/sitzungen/{sid}/ausfuehren` mit `{sprache, quelltext, eingabe?}`, nur in der eigenen
  Sitzung. Antwort: `{exit, stdout, stderr, zeit_ueberschritten, abgeschnitten, dauer_ms}`. Gebucht wird nichts.
- Seite: bei Programmieraufgaben Sprachwahl, „Ausführen“ und ein Ausgabefeld unter dem Antwortfeld.

## Tests

`tests/huelle/test_laeufer.py` und `tests/huelle/test_code_ausfuehren.py`. Die echten Läufe (Abschottung, Grenzen)
laufen nur mit `LAEUFER_LIVE=1` und eingerichteter Distro.

## Programmieraufgaben mit Testfällen

Die Werkstatt kennt den Aufgabentyp `code`: Sprache (Python oder JavaScript), optionaler Startcode, Musterlösung,
bis zu zehn Testfälle und eine Rubrik. Ein Testfall hat einen Namen, eine Eingabe (stdin) und die erwartete Ausgabe
(stdout). Verglichen wird nach vereinheitlichten Zeilenenden, ohne Leerraum am Zeilenende und am Textende
(`services/evaluation/code_tests.py`).

Code läuft nur am PC. Daraus folgen zwei Wege:

| Wann | Wer führt aus | Was passiert |
|---|---|---|
| Anlegen | Learning-Claw (`werkstatt_code_pruefen`) | Die Musterlösung läuft gegen die Testfälle. Das Ergebnis steht als `testlauf` im Inhalt der Fassung, gebunden an Sprache, Musterlösung und Tests (Fingerabdruck). |
| Abgeben | Hülle (`bruecke._code_testlauf`) | Die Hülle holt die Testfälle bei Learning (`learning_task_bewertungsdaten`, `art: "code"`), lässt den abgegebenen Code laufen und schickt das Ergebnis als Feld `testlauf` neben `answer`. |

Regeln:

- Ohne passenden, bestandenen `testlauf` hat die Aufgabe den Befund `code_ungeprueft` oder
  `code_musterloesung_falsch` und ist für die Freigabe gesperrt. Die Freigabe berechnet das aus dem Inhalt neu.
- Das Autorenmodell kann keinen `testlauf` setzen; ein behaupteter wird verworfen.
- Lernende sehen Sprache und Startcode, nie die Testfälle oder die Musterlösung.
- Ein `testlauf` im Antwortfeld zählt nicht (das Feld kommt aus dem Browser). Es gilt nur das Feld, das die Hülle setzt.
- Bestehen nicht alle Tests oder fehlt der Testlauf, bleibt die Punktzahl knapp unter der Grenze für „richtig“.
- Die Rückmeldung beginnt mit den Tests und nennt die Namen der gescheiterten.
- Ist der Läufer beim Abgeben nicht erreichbar, wird nicht abgegeben (503).
- Ältere `code`-Aufgaben ohne Testfälle verhalten sich wie bisher.

## Erlaubnisse

Ein Lauf darf zunächst nur rechnen und Text ausgeben. Vier Erlaubnisse öffnen einzelne Dinge; das Netz bleibt
immer zu.

| Kennung | Öffnet | Durchsetzung |
|---|---|---|
| `pakete` | `numpy`, `matplotlib` (Python) | Ohne sie liegt im Lauf ein leeres tmpfs über dem Paketordner von Python. |
| `diagramm` | bis zu 3 PNG-Bilder (je höchstens 1,5 MB) aus dem Ordner `ausgabe` | `aufseher.py` läuft in der Sandbox, startet das Programm und reicht danach geprüfte Bilder über einen eigenen Kanal hinaus; `lauf.py` und der Host prüfen sie erneut. |
| `lange_laufzeit` | 20 statt 5 Sekunden | Host und `lauf.py` lehnen `zeit_s > 5` ohne sie ab. |
| `daten` | Textdateien der Aufgabe unter `daten/`, nur lesend | Die Dateien kommen mit dem Auftrag und werden je Lauf angelegt (höchstens 5, zusammen 200.000 Zeichen). |

Wer was festlegt:

- **Obergrenze je Aufgabe:** `erlaubnisse` (und `dateien`) stehen an der Werkstatt-Aufgabe und werden mit ihr
  freigegeben. Lernende sehen die Erlaubnisse und die Dateinamen (`presentation`), nicht die Dateiinhalte.
- **Zustimmung der Person:** Die Seite erkennt am Code, was er braucht (Import von `numpy`/`matplotlib`, die
  Ordnernamen `ausgabe/` und `daten/`), und fragt vor dem Lauf und vor der Abgabe: einmal erlauben, für diese
  Aufgabe erlauben, ablehnen. Läuft die Zeit ab, bietet sie die längere Laufzeit an.
- **Was gilt:** die Zustimmung, geschnitten mit der Obergrenze. Die Brücke liest Obergrenze und Dateien bei
  Learning (`learning_task_bewertungsdaten`), nie aus dem Body. Die Antwort von `/ausfuehren` nennt in `erlaubt`,
  was galt, und trägt die Bilder in `bilder`.
- **Musterlösung:** `werkstatt_code_pruefen` lässt sie mit der ganzen Obergrenze laufen. Der Fingerabdruck des
  Testlaufs schließt Erlaubnisse und Dateien ein, sobald es welche gibt.
- **Regelliste je Kurs:** Die Inhaberin legt je Erlaubnis eine Stufe fest (`code_regeln` am Werkstatt-Kurs,
  gesetzt über `werkstatt_kurs_aendern`). Ohne Eintrag gilt „mit Frage“.

| Stufe | Wirkung |
|---|---|
| `ohne_frage` | Was die Aufgabe vorsieht, gilt bei jedem Lauf ohne Rückfrage (`presentation.ohne_frage`). Die Karte nennt es in einer Zeile. |
| `mit_frage` | Die Person wird vor dem Lauf gefragt (Grundzustand, wird nicht gespeichert). |
| `nie` | Die Erlaubnis gibt es im Kurs nicht. Eine Aufgabe, die sie vorsieht, sperrt die Freigabe (`code_erlaubnis_verboten`), statt still unlösbar zu werden. |

Die Regeln heben nie an, was eine Aufgabe vorsieht. Sie gelten erst mit einer Freigabe: Eine geänderte Liste ist
für sich ein Freigabe-Vorschlag (`vergleich.code_regeln_vorher`), die Freigabeseite nennt jede geänderte Stufe,
und ausgeliefert wird der Stand, der dort gezeigt wurde (`vergleich.code_regeln`), nicht ein späterer.

Die Rückfrage an die Person macht sichtbar, was ihr Code tut. Den Schutz liefern die Obergrenze und die
Abschottung: der Code läuft auf dem PC der Kursleitung, nicht auf dem Gerät der Person.

Gemessen am 16.11.2026: `import numpy` 0,1 s, ein Diagramm 0,4–0,7 s, beides innerhalb der 512 MB je Lauf.

Die Seite zeigt Diagramme als `blob:`-Adressen; ihre Richtlinie erlaubt dafür `img-src 'self' blob:`.

## Offen

- Bild-KI als Hilfsfunktion (ein Bild dauert rund zwei Minuten und passt nicht in einen Lauf).
- Weitere Pakete; Pakete für JavaScript.
- Tests vergleichen nur die Textausgabe; Diagramme sieht die bewertende KI nicht.
- Im Prüfungsmodus ist die Ausführung nicht gesondert geregelt (die Hülle startet bisher nur Übungen); Learning gibt
  dort keine Testfälle heraus.
- `code`-Aufgaben haben keine Vorbewertung: die Abgabe wird sofort gebucht, es gibt keinen zweiten Versuch.
