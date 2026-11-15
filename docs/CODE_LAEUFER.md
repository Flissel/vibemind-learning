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

## Offen

- Bewertung durch Ausführen: Tests je Aufgabe, deren Ergebnis in die Bewertung eingeht. Heute bewertet weiter die
  Rubrik den Text.
- Die Sprache steht noch nicht an der Aufgabe; die Person wählt sie selbst.
- Im Prüfungsmodus ist die Ausführung nicht gesondert geregelt (die Hülle startet bisher nur Übungen).
