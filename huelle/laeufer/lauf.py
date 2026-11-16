"""Laeuft IN der WSL-Distro `vibemind-code` als Benutzer `laeufer`: ein Auftrag (JSON auf stdin) -> ein Ergebnis
(JSON auf stdout). Nur Standardbibliothek; `einrichten.ps1` kopiert diese Datei nach /usr/local/lib/vibemind/.

Abschottung je Lauf (bubblewrap):
- kein Netz (`--unshare-all`): WSL teilt im Modus `mirrored` das Netz des PCs, ohne Sperre waere 127.0.0.1 des PCs
  erreichbar (Huelle, Lernflaeche, Tresor-Dienste);
- nur /usr, /lib, /bin lesend; kein /home, kein /etc, kein /mnt; leere Umgebung;
- /work und /tmp sind kleine tmpfs, der Quelltext liegt lesend in /work;
- eigene PID-Welt, stirbt mit dem Aufrufer; Grenzen fuer Zeit, Speicher, Dateigroesse, Prozesse.

Erlaubnisse (alles Weitere ist zu, das Netz immer):
- `pakete`: ohne sie liegt ein leeres tmpfs ueber dem Paketordner von Python (numpy, matplotlib sind dann nicht da);
- `diagramm`: `aufseher.py` reicht nach dem Lauf bis zu drei PNG-Bilder aus /work/ausgabe heraus;
- `lange_laufzeit`: mehr als STANDARD_MAX_ZEIT_S Sekunden (bis MAX_ZEIT_S);
- `daten`: die Dateien des Auftrags liegen lesend unter /work/daten.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

SPRACHEN = {"python": "main.py", "javascript": "main.js"}
ERLAUBNISSE = ("pakete", "diagramm", "lange_laufzeit", "daten")
MAX_QUELLTEXT = 50_000
MAX_EINGABE = 100_000
STANDARD_MAX_ZEIT_S = 5       # ohne `lange_laufzeit`
MAX_ZEIT_S = 20
MAX_AUSGABE = 20_000          # Zeichen je Kanal im Ergebnis
SPEICHER_BYTES = 512 * 1024**2
DATEI_BYTES = 8 * 1024**2     # auch die mitgeschnittene Ausgabe und der Bildkanal
PROZESSE = 64
TMPFS_BYTES = 16 * 1024**2
MAX_DATEIEN = 5
MAX_DATEIEN_ZEICHEN = 200_000  # alle Uebungsdateien zusammen
MAX_BILDER = 3
BILD_BYTES = 1_500_000
PNG_KENNUNG = b"\x89PNG\r\n\x1a\n"
AUFSEHER = "/usr/local/lib/vibemind/aufseher.py"
_DATEINAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,59}$")
_BILDNAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,59}\.png$")


@dataclass(frozen=True)
class Auftrag:
    sprache: str
    quelltext: str
    eingabe: str
    zeit_s: int
    erlaubnisse: tuple[str, ...] = ()
    dateien: tuple[tuple[str, str], ...] = ()   # (name, inhalt)


def pruefen(auftrag: object) -> Auftrag:
    """Der gepruefte Auftrag oder ValueError. Derselbe Rahmen wie im Host."""
    if not isinstance(auftrag, dict):
        raise ValueError("auftrag")
    sprache, quelltext = auftrag.get("sprache"), auftrag.get("quelltext")
    eingabe, zeit_s = auftrag.get("eingabe", ""), auftrag.get("zeit_s")
    if sprache not in SPRACHEN:
        raise ValueError("sprache")
    if not isinstance(quelltext, str) or not quelltext or len(quelltext) > MAX_QUELLTEXT or "\x00" in quelltext:
        raise ValueError("quelltext")
    if not isinstance(eingabe, str) or len(eingabe) > MAX_EINGABE:
        raise ValueError("eingabe")
    erlaubnisse = auftrag.get("erlaubnisse", [])
    if (not isinstance(erlaubnisse, list) or any(e not in ERLAUBNISSE for e in erlaubnisse)
            or len(set(erlaubnisse)) != len(erlaubnisse)):
        raise ValueError("erlaubnisse")
    grenze = MAX_ZEIT_S if "lange_laufzeit" in erlaubnisse else STANDARD_MAX_ZEIT_S
    if isinstance(zeit_s, bool) or not isinstance(zeit_s, int) or not 1 <= zeit_s <= grenze:
        raise ValueError("zeit_s")
    return Auftrag(sprache, quelltext, eingabe, zeit_s, tuple(erlaubnisse),
                   _dateien(auftrag.get("dateien", []), "daten" in erlaubnisse))


def _dateien(roh: object, erlaubt: bool) -> tuple[tuple[str, str], ...]:
    if not isinstance(roh, list) or len(roh) > MAX_DATEIEN or (roh and not erlaubt):
        raise ValueError("dateien")
    dateien: list[tuple[str, str]] = []
    for eintrag in roh:
        name = eintrag.get("name") if isinstance(eintrag, dict) else None
        inhalt = eintrag.get("inhalt") if isinstance(eintrag, dict) else None
        if not isinstance(name, str) or not _DATEINAME.match(name) or not isinstance(inhalt, str) or "\x00" in inhalt:
            raise ValueError("dateien")
        dateien.append((name, inhalt))
    if len({n for n, _ in dateien}) != len(dateien) or sum(len(i) for _, i in dateien) > MAX_DATEIEN_ZEICHEN:
        raise ValueError("dateien")
    return tuple(dateien)


def paketordner() -> tuple[str, ...]:
    """Wo apk die Python-Pakete ablegt (numpy, matplotlib und ihre Begleiter); die Standardbibliothek liegt daneben."""
    return tuple(sorted(glob.glob("/usr/lib/python3*/site-packages")))


def befehl(
    sprache: str, quelldatei: str, erlaubnisse: tuple[str, ...] = (), *,
    paketordner: tuple[str, ...] = (), datenordner: str | None = None, bildkanal: int | None = None,
) -> list[str]:
    """bubblewrap-Aufruf fuer `quelldatei` (liegt ausserhalb der Sandbox, wird lesend nach /work gebunden)."""
    name = SPRACHEN[sprache]
    start = ["python3", name] if sprache == "python" else \
        ["node", f"--max-old-space-size={SPEICHER_BYTES // 1024**2 // 2}", name]
    teile = [
        "bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib", "--ro-bind", "/bin", "/bin",
    ]
    umgebung = ["--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8"]
    if "pakete" in erlaubnisse:
        # matplotlib ohne Bildschirm und mit beschreibbarem Zwischenspeicher; numpy mit einem Rechenfaden.
        umgebung += ["--setenv", "MPLBACKEND", "Agg", "--setenv", "MPLCONFIGDIR", "/tmp",
                     "--setenv", "OPENBLAS_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1"]
    else:
        # Nach dem Einbinden von /usr: ein leeres tmpfs verdeckt den Paketordner.
        for ordner in paketordner:
            teile += ["--size", "65536", "--tmpfs", ordner]
    teile += [
        "--proc", "/proc", "--dev", "/dev",
        "--size", str(TMPFS_BYTES), "--tmpfs", "/tmp",
        "--size", str(TMPFS_BYTES), "--tmpfs", "/work",
        "--ro-bind", quelldatei, f"/work/{name}",
    ]
    if "daten" in erlaubnisse and datenordner is not None:
        teile += ["--ro-bind", datenordner, "/work/daten"]
    teile += ["--chdir", "/work", "--clearenv", *umgebung]
    if "diagramm" in erlaubnisse and bildkanal is not None:
        return [*teile, "python3", AUFSEHER, str(bildkanal), *start]
    return [*teile, *start]


def kuerzen(text: str, grenze: int = MAX_AUSGABE) -> tuple[str, bool]:
    return (text, False) if len(text) <= grenze else (text[:grenze], True)


def _grenzen(sprache: str, zeit_s: int):
    def setzen() -> None:
        import resource  # nur unter Linux vorhanden
        resource.setrlimit(resource.RLIMIT_CPU, (zeit_s + 1, zeit_s + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (DATEI_BYTES, DATEI_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (PROZESSE, PROZESSE))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if sprache == "python":  # node reserviert riesige Adressbereiche; dort begrenzt --max-old-space-size
            resource.setrlimit(resource.RLIMIT_AS, (SPEICHER_BYTES, SPEICHER_BYTES))
    return setzen


def _lesen(pfad: str) -> str:
    with open(pfad, "rb") as datei:
        return datei.read(MAX_AUSGABE * 4 + 4).decode("utf-8", "replace")


def bilder_lesen(pfad: str) -> list[dict[str, str]]:
    """Was der Aufseher auf den Bildkanal schrieb, noch einmal geprueft: Name, PNG-Kennung, Groesse, Anzahl.
    Der Kanal liegt in der Sandbox offen (/proc), sein Inhalt gilt deshalb als Eingabe der Person."""
    bilder: list[dict[str, str]] = []
    try:
        with open(pfad, "rb") as datei:
            zeilen = datei.read(DATEI_BYTES + 1).splitlines()
    except OSError:
        return bilder
    for zeile in zeilen:
        if len(bilder) == MAX_BILDER:
            break
        try:
            eintrag = json.loads(zeile)
            name, b64 = eintrag["name"], eintrag["png_base64"]
            roh = base64.b64decode(b64, validate=True)
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(name, str) and _BILDNAME.match(name) and len(roh) <= BILD_BYTES and roh.startswith(PNG_KENNUNG):
            bilder.append({"name": name, "png_base64": base64.b64encode(roh).decode("ascii")})
    return bilder


def laufen(auftrag: Auftrag) -> dict[str, object]:
    sprache, zeit_s, erlaubnisse = auftrag.sprache, auftrag.zeit_s, auftrag.erlaubnisse
    ordner = tempfile.mkdtemp(prefix="lauf-")
    try:
        quelldatei = os.path.join(ordner, SPRACHEN[sprache])
        with open(quelldatei, "w", encoding="utf-8", newline="\n") as datei:
            datei.write(auftrag.quelltext)
        datenordner = None
        if "daten" in erlaubnisse:
            datenordner = os.path.join(ordner, "daten")
            os.mkdir(datenordner)
            for name, inhalt in auftrag.dateien:
                with open(os.path.join(datenordner, name), "w", encoding="utf-8", newline="\n") as datei:
                    datei.write(inhalt)
        aus, fehler = os.path.join(ordner, "stdout"), os.path.join(ordner, "stderr")
        bildpfad = os.path.join(ordner, "bilder")
        kanal = os.open(bildpfad, os.O_WRONLY | os.O_CREAT, 0o600) if "diagramm" in erlaubnisse else None
        beginn = time.monotonic()
        zeit_ueberschritten = False
        try:
            with open(aus, "wb") as o, open(fehler, "wb") as e:
                prozess = subprocess.Popen(
                    befehl(sprache, quelldatei, erlaubnisse, paketordner=paketordner(), datenordner=datenordner,
                           bildkanal=kanal),
                    stdin=subprocess.PIPE, stdout=o, stderr=e, preexec_fn=_grenzen(sprache, zeit_s),
                    start_new_session=True, pass_fds=() if kanal is None else (kanal,),
                )
                try:
                    prozess.communicate(auftrag.eingabe.encode("utf-8"), timeout=zeit_s)
                except subprocess.TimeoutExpired:
                    zeit_ueberschritten = True
                    try:
                        os.killpg(prozess.pid, 9)  # bwrap stirbt, mit ihm (--die-with-parent, PID-Welt) alles darin
                    except ProcessLookupError:
                        pass
                    prozess.wait()
        finally:
            if kanal is not None:
                os.close(kanal)
        dauer_ms = int((time.monotonic() - beginn) * 1000)
        stdout, ab1 = kuerzen(_lesen(aus))
        stderr, ab2 = kuerzen(_lesen(fehler))
        code = prozess.returncode
        if code is not None and code < 0 and -code == 24:  # SIGXCPU: Rechenzeit verbraucht
            zeit_ueberschritten = True
        ergebnis: dict[str, object] = {
            "exit": None if zeit_ueberschritten else code, "stdout": stdout, "stderr": stderr,
            "zeit_ueberschritten": zeit_ueberschritten, "abgeschnitten": ab1 or ab2, "dauer_ms": dauer_ms,
        }
        if kanal is not None:
            ergebnis["bilder"] = bilder_lesen(bildpfad)
        return ergebnis
    finally:
        shutil.rmtree(ordner, ignore_errors=True)


def main() -> int:
    grenze = 4 * (MAX_QUELLTEXT + MAX_EINGABE + MAX_DATEIEN_ZEICHEN) + 2000
    try:
        auftrag = pruefen(json.loads(sys.stdin.buffer.read(grenze).decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as fehler:
        print(json.dumps({"fehler": f"auftrag:{fehler}"}))
        return 2
    print(json.dumps(laufen(auftrag)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
