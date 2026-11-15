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

## Offen

- Erlaubnisse je Lauf (Pakete, Diagramme, weitere Hilfsfunktionen) mit Rückfrage an die Person.
- Im Prüfungsmodus ist die Ausführung nicht gesondert geregelt (die Hülle startet bisher nur Übungen); Learning gibt
  dort keine Testfälle heraus.
- `code`-Aufgaben haben keine Vorbewertung: die Abgabe wird sofort gebucht, es gibt keinen zweiten Versuch.
