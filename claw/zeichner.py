"""Draws a diagram with Graphviz on this machine: PNG for the task, SVG inside one HTML page.

The Graphviz text comes from `services/werkstatt/diagramm.py` and goes in through standard input; nothing
here writes a model's text into a command line or a file name.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

ORDNER_ENV = "LEARNING_DIAGRAMM_ORDNER"
FRIST_S = 10
MAX_BYTES = 580_000
MAX_BREITE = 1000
_ARTEN = ("png", "svg")
_PNG = b"\x89PNG\r\n\x1a\n"
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
# Variables that make Graphviz look for pictures or fonts in folders of our choosing; never passed on.
_NICHT_ERBEN = frozenset({"GV_FILE_PATH", "GDFONTPATH", "DOTFONTPATH", "SERVER_NAME"})


class ZeichnerFehler(RuntimeError):
    """Drawing failed; the message is meant for the owner."""


def dot_pfad() -> str | None:
    """Where Graphviz' `dot` is: on the search path, else in its usual Windows folder."""
    gefunden = shutil.which("dot")
    if gefunden:
        return gefunden
    ueblich = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Graphviz" / "bin" / "dot.exe"
    return str(ueblich) if ueblich.is_file() else None


def rendern(dot: str, art: str) -> bytes:
    """`dot` drawn as `art` ("png" or "svg"). Raises `ZeichnerFehler` with a sentence for the owner."""
    if art not in _ARTEN:
        raise ZeichnerFehler("Gezeichnet wird nur als PNG oder SVG")
    programm = dot_pfad()
    if programm is None:
        raise ZeichnerFehler("Graphviz ist auf diesem Rechner nicht eingerichtet")
    umgebung = {name: wert for name, wert in os.environ.items() if name.upper() not in _NICHT_ERBEN}
    try:
        lauf = subprocess.run(  # noqa: S603 - fixed program, fixed arguments, text only on stdin
            [programm, f"-T{art}"], input=dot.encode("utf-8"), capture_output=True, timeout=FRIST_S,
            env=umgebung, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as fehler:
        raise ZeichnerFehler("Das Zeichnen hat zu lange gedauert; das Diagramm ist zu groß") from fehler
    except OSError as fehler:
        raise ZeichnerFehler("Graphviz ließ sich nicht starten") from fehler
    if lauf.returncode != 0 or not lauf.stdout:
        raise ZeichnerFehler("Graphviz konnte das Diagramm nicht zeichnen")
    return lauf.stdout


def png_masse(daten: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG header; None if `daten` is not a PNG."""
    if len(daten) < 24 or not daten.startswith(_PNG) or daten[12:16] != b"IHDR":
        return None
    breite, hoehe = struct.unpack(">II", daten[16:24])
    return (breite, hoehe) if breite and hoehe else None


def ordner() -> Path:
    """Where the HTML pages go: LEARNING_DIAGRAMM_ORDNER, else `diagramme` next to the claw's env file,
    else in the temp folder."""
    gesetzt = os.environ.get(ORDNER_ENV, "").strip()
    if gesetzt:
        return Path(gesetzt)
    env_datei = os.environ.get("LEARNING_CLAW_ENV_FILE", "").strip()
    basis = Path(env_datei).parent if env_datei else Path(tempfile.gettempdir())
    return basis / "diagramme"


def dateiname(titel: str, inhalt: bytes) -> str:
    """`<titel-in-kleinbuchstaben>-<8 hex of the content>.html`; only a-z, 0-9 and hyphens survive."""
    glatt = re.sub(r"[^a-z0-9]+", "-", titel.lower().translate(_UMLAUT)).strip("-")[:60].strip("-")
    return f"{glatt or 'diagramm'}-{hashlib.sha256(inhalt).hexdigest()[:8]}.html"


def html_seite(titel: str, svg: bytes) -> str:
    """One self-contained page: the title and the SVG, no script and nothing fetched. That is what an
    HTML concept without network access can show."""
    text = svg.decode("utf-8")
    anfang = text.find("<svg")
    if anfang < 0 or "<script" in text.lower():
        raise ZeichnerFehler("Graphviz hat kein brauchbares SVG geliefert")
    sicher = html.escape(titel, quote=True)
    return (
        "<!doctype html>\n"
        '<html lang="de">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{sicher}</title>\n"
        "<style>body{margin:24px;font-family:system-ui,sans-serif;background:#fff;color:#111}"
        "h1{font-size:1.3rem;margin:0 0 16px}svg{max-width:100%;height:auto}</style>\n"
        f"</head>\n<body>\n<h1>{sicher}</h1>\n{text[anfang:].strip()}\n</body>\n</html>\n"
    )


def html_ablegen(titel: str, svg: bytes) -> Path:
    """Writes the page into `ordner()` and returns where it is."""
    seite = html_seite(titel, svg).encode("utf-8")
    ziel = ordner()
    ziel.mkdir(parents=True, exist_ok=True)
    pfad = ziel / dateiname(titel, seite)
    pfad.write_bytes(seite)
    return pfad
