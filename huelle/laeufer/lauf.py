"""Laeuft IN der WSL-Distro `vibemind-code` als Benutzer `laeufer`: ein Auftrag (JSON auf stdin) -> ein Ergebnis
(JSON auf stdout). Nur Standardbibliothek; `einrichten.ps1` kopiert diese Datei nach /usr/local/lib/vibemind/.

Abschottung je Lauf (bubblewrap):
- kein Netz (`--unshare-all`): WSL teilt im Modus `mirrored` das Netz des PCs, ohne Sperre waere 127.0.0.1 des PCs
  erreichbar (Huelle, Lernflaeche, Tresor-Dienste);
- nur /usr, /lib, /bin lesend; kein /home, kein /etc, kein /mnt; leere Umgebung;
- /work und /tmp sind kleine tmpfs, der Quelltext liegt lesend in /work;
- eigene PID-Welt, stirbt mit dem Aufrufer; Grenzen fuer Zeit, Speicher, Dateigroesse, Prozesse.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

SPRACHEN = {"python": "main.py", "javascript": "main.js"}
MAX_QUELLTEXT = 50_000
MAX_EINGABE = 100_000
MAX_ZEIT_S = 20
MAX_AUSGABE = 20_000          # Zeichen je Kanal im Ergebnis
SPEICHER_BYTES = 512 * 1024**2
DATEI_BYTES = 8 * 1024**2     # auch die mitgeschnittene Ausgabe
PROZESSE = 64
TMPFS_BYTES = 16 * 1024**2


def pruefen(auftrag: object) -> tuple[str, str, str, int]:
    """(sprache, quelltext, eingabe, zeit_s) oder ValueError. Derselbe Rahmen wie im Host."""
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
    if isinstance(zeit_s, bool) or not isinstance(zeit_s, int) or not 1 <= zeit_s <= MAX_ZEIT_S:
        raise ValueError("zeit_s")
    return sprache, quelltext, eingabe, zeit_s


def befehl(sprache: str, quelldatei: str) -> list[str]:
    """bubblewrap-Aufruf fuer `quelldatei` (liegt ausserhalb der Sandbox, wird lesend nach /work gebunden)."""
    name = SPRACHEN[sprache]
    start = ["python3", name] if sprache == "python" else \
        ["node", f"--max-old-space-size={SPEICHER_BYTES // 1024**2 // 2}", name]
    return [
        "bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib", "--ro-bind", "/bin", "/bin",
        "--proc", "/proc", "--dev", "/dev",
        "--size", str(TMPFS_BYTES), "--tmpfs", "/tmp",
        "--size", str(TMPFS_BYTES), "--tmpfs", "/work",
        "--ro-bind", quelldatei, f"/work/{name}",
        "--chdir", "/work", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
        *start,
    ]


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


def laufen(sprache: str, quelltext: str, eingabe: str, zeit_s: int) -> dict[str, object]:
    ordner = tempfile.mkdtemp(prefix="lauf-")
    try:
        quelldatei = os.path.join(ordner, SPRACHEN[sprache])
        with open(quelldatei, "w", encoding="utf-8", newline="\n") as datei:
            datei.write(quelltext)
        aus, fehler = os.path.join(ordner, "stdout"), os.path.join(ordner, "stderr")
        beginn = time.monotonic()
        zeit_ueberschritten = False
        with open(aus, "wb") as o, open(fehler, "wb") as e:
            prozess = subprocess.Popen(befehl(sprache, quelldatei), stdin=subprocess.PIPE, stdout=o, stderr=e,
                                       preexec_fn=_grenzen(sprache, zeit_s), start_new_session=True)
            try:
                prozess.communicate(eingabe.encode("utf-8"), timeout=zeit_s)
            except subprocess.TimeoutExpired:
                zeit_ueberschritten = True
                try:
                    os.killpg(prozess.pid, 9)  # bwrap stirbt, mit ihm (--die-with-parent, PID-Welt) alles darin
                except ProcessLookupError:
                    pass
                prozess.wait()
        dauer_ms = int((time.monotonic() - beginn) * 1000)
        stdout, ab1 = kuerzen(_lesen(aus))
        stderr, ab2 = kuerzen(_lesen(fehler))
        code = prozess.returncode
        if code is not None and code < 0 and -code == 24:  # SIGXCPU: Rechenzeit verbraucht
            zeit_ueberschritten = True
        return {"exit": None if zeit_ueberschritten else code, "stdout": stdout, "stderr": stderr,
                "zeit_ueberschritten": zeit_ueberschritten, "abgeschnitten": ab1 or ab2, "dauer_ms": dauer_ms}
    finally:
        shutil.rmtree(ordner, ignore_errors=True)


def main() -> int:
    try:
        auftrag = pruefen(json.loads(sys.stdin.buffer.read(4 * (MAX_QUELLTEXT + MAX_EINGABE) + 1000).decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as fehler:
        print(json.dumps({"fehler": f"auftrag:{fehler}"}))
        return 2
    print(json.dumps(laufen(*auftrag)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
