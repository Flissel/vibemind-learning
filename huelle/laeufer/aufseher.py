"""Laeuft IN der Sandbox, nur mit der Erlaubnis `diagramm`: startet das Programm der Person, wartet auf sein Ende
und reicht danach die Bilder aus `/work/ausgabe` ueber einen eigenen Kanal (Dateinummer) nach aussen.

Warum so: `/work` ist ein kleines tmpfs innerhalb der Sandbox und verschwindet mit ihr. Ein von aussen
eingebundener Ordner haette keine Groessengrenze. So bleibt die Grenze des tmpfs bestehen, und nach aussen gehen
nur gepruefte Bytes. Nur Standardbibliothek; `einrichten.ps1` kopiert diese Datei nach /usr/local/lib/vibemind/.

Aufruf: python3 aufseher.py <dateinummer> <programm> [argumente ...]
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys

AUSGABE = "/work/ausgabe"
MAX_BILDER = 3
BILD_BYTES = 1_500_000
PNG_KENNUNG = b"\x89PNG\r\n\x1a\n"
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,59}\.png$")


def bilder_sammeln(ordner: str = AUSGABE) -> list[tuple[str, bytes]]:
    """Die ersten MAX_BILDER PNG-Dateien (nach Namen): nur regulaere Dateien, keine Verweise, mit PNG-Kennung
    und hoechstens BILD_BYTES gross. Alles andere bleibt liegen."""
    try:
        eintraege = sorted(os.scandir(ordner), key=lambda e: e.name)
    except OSError:
        return []
    bilder: list[tuple[str, bytes]] = []
    for eintrag in eintraege:
        if len(bilder) == MAX_BILDER:
            break
        try:
            if not _NAME.match(eintrag.name) or not eintrag.is_file(follow_symlinks=False):
                continue
            if eintrag.stat(follow_symlinks=False).st_size > BILD_BYTES:
                continue
            with open(eintrag.path, "rb") as datei:
                roh = datei.read(BILD_BYTES + 1)
        except OSError:
            continue
        if len(roh) <= BILD_BYTES and roh.startswith(PNG_KENNUNG):
            bilder.append((eintrag.name, roh))
    return bilder


def main() -> int:
    kanal = int(sys.argv[1])
    try:
        os.makedirs(AUSGABE, exist_ok=True)
    except OSError:
        pass
    # Das Programm erbt den Kanal nicht (close_fds); Ein- und Ausgabe gehen unveraendert durch.
    code = subprocess.run(sys.argv[2:], close_fds=True).returncode
    try:
        with os.fdopen(kanal, "w", encoding="ascii") as aus:
            for name, roh in bilder_sammeln():
                aus.write(json.dumps({"name": name, "png_base64": base64.b64encode(roh).decode("ascii")}) + "\n")
    except OSError:
        pass
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
