"""Code-Laeufer: Auftrag pruefen, Sandbox-Befehl, Aufruf der Distro. Der echte Lauf braucht die WSL-Distro
`vibemind-code` und laeuft nur mit LAEUFER_LIVE=1 (siehe huelle/laeufer/einrichten.ps1)."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from spaces.learning.huelle.laeufer import (
    DISTRO, Lauf, LaeuferNichtBereit, UngueltigerAuftrag, ausfuehren,
)
from spaces.learning.huelle.laeufer import lauf


class Starter:
    """Ersetzt subprocess.run; merkt sich den Aufruf."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, fehler: Exception | None = None) -> None:
        self.stdout, self.returncode, self.fehler = stdout, returncode, fehler
        self.aufrufe: list[tuple[list[str], dict]] = []

    def __call__(self, befehl, **kw):
        self.aufrufe.append((list(befehl), kw))
        if self.fehler is not None:
            raise self.fehler
        return subprocess.CompletedProcess(befehl, self.returncode, self.stdout, b"")


def _antwort(**felder) -> bytes:
    basis = {"exit": 0, "stdout": "", "stderr": "", "zeit_ueberschritten": False, "abgeschnitten": False,
             "dauer_ms": 12}
    return json.dumps({**basis, **felder}).encode()


# ---- Host: Auftrag pruefen -------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [
    {"sprache": "bash", "quelltext": "echo hi"},           # nur python und javascript
    {"sprache": "python", "quelltext": ""},                # leer
    {"sprache": "python", "quelltext": "x" * 50_001},      # zu lang
    {"sprache": "python", "quelltext": "x", "eingabe": "y" * 100_001},
    {"sprache": "python", "quelltext": "x", "zeit_s": 0},
    {"sprache": "python", "quelltext": "x", "zeit_s": 21},
    {"sprache": "python", "quelltext": "x\x00y"},          # NUL im Quelltext
])
def test_ungueltiger_auftrag_startet_nichts(kw):
    starter = Starter(_antwort())
    with pytest.raises(UngueltigerAuftrag):
        ausfuehren(starter=starter, **kw)
    assert starter.aufrufe == []


# ---- Host: Aufruf der Distro -----------------------------------------------------------------------

def test_aufruf_geht_als_laeufer_in_die_distro_und_der_code_nur_ueber_stdin():
    starter = Starter(_antwort(stdout="42\n"))
    ergebnis = ausfuehren("python", "print(6*7)", eingabe="a\n", zeit_s=3, starter=starter)
    assert ergebnis == Lauf(exit_code=0, stdout="42\n", stderr="", zeit_ueberschritten=False,
                            abgeschnitten=False, dauer_ms=12)
    befehl, kw = starter.aufrufe[0]
    assert befehl[:6] == ["wsl.exe", "-d", DISTRO, "-u", "laeufer", "--cd"]
    assert "print(6*7)" not in " ".join(befehl)  # nie auf der Kommandozeile (Laenge, Quoting, Prozessliste)
    assert json.loads(kw["input"]) == {"sprache": "python", "quelltext": "print(6*7)", "eingabe": "a\n", "zeit_s": 3}
    assert kw["timeout"] > 3  # der Host wartet laenger als der Lauf dauern darf
    assert kw["shell"] is False


def test_distro_fehlt_oder_antwortet_unbrauchbar():
    for starter in (Starter(fehler=FileNotFoundError()), Starter(b"", returncode=1), Starter(b"kein json"),
                    Starter(json.dumps({"exit": 0}).encode()), Starter(fehler=subprocess.TimeoutExpired("wsl", 1))):
        with pytest.raises(LaeuferNichtBereit):
            ausfuehren("python", "print(1)", starter=starter)


def test_zeitueberschreitung_ist_ein_ergebnis_kein_fehler():
    starter = Starter(_antwort(exit=None, zeit_ueberschritten=True))
    ergebnis = ausfuehren("javascript", "for(;;){}", starter=starter)
    assert ergebnis.zeit_ueberschritten and ergebnis.exit_code is None


# ---- In der Distro: lauf.py ------------------------------------------------------------------------

def test_sandbox_befehl_ohne_netz_ohne_heimat_mit_kleinem_arbeitsbereich():
    befehl = lauf.befehl("python", "/tmp/x/main.py")
    assert befehl[0] == "bwrap"
    assert "--unshare-all" in befehl and "--share-net" not in befehl  # kein Netz: WSL teilt sonst das Netz des PCs
    assert "--die-with-parent" in befehl and "--clearenv" in befehl
    assert "--disable-userns" in befehl  # keine verschachtelten Namensraeume (weniger Angriffsflaeche am Kernel)
    text = " ".join(befehl)
    assert "/home" not in text and "/mnt" not in text and "/etc" not in text
    assert "--ro-bind /tmp/x/main.py /work/main.py" in text
    assert "--size" in befehl  # /work und /tmp sind begrenzte tmpfs
    assert befehl[-2:] == ["python3", "main.py"]


def test_sandbox_befehl_javascript_mit_speichergrenze():
    befehl = lauf.befehl("javascript", "/tmp/x/main.js")
    assert befehl[-3] == "node" and befehl[-2].startswith("--max-old-space-size=") and befehl[-1] == "main.js"


def test_lauf_py_prueft_selbst_noch_einmal():
    # Die Distro traut dem Host nicht blind: derselbe Rahmen wie im Host.
    for auftrag in ({"sprache": "bash", "quelltext": "x", "eingabe": "", "zeit_s": 1},
                    {"sprache": "python", "quelltext": "x", "eingabe": "", "zeit_s": 99},
                    {"sprache": "python", "quelltext": 5, "eingabe": "", "zeit_s": 1}, "kein objekt"):
        with pytest.raises(ValueError):
            lauf.pruefen(auftrag)
    assert lauf.pruefen({"sprache": "python", "quelltext": "x", "eingabe": "", "zeit_s": 2}) == ("python", "x", "", 2)


def test_kuerzen_meldet_abgeschnitten():
    assert lauf.kuerzen("abc", 10) == ("abc", False)
    assert lauf.kuerzen("x" * 20, 10) == ("x" * 10, True)


# ---- Echt: nur mit eingerichteter Distro -----------------------------------------------------------

live = pytest.mark.skipif(os.environ.get("LAEUFER_LIVE") != "1", reason="braucht die Distro vibemind-code")


@live
def test_live_python_und_javascript():
    assert ausfuehren("python", "print(sum(range(10)))").stdout == "45\n"
    assert ausfuehren("javascript", "console.log([1,2,3].map(x=>x*2).join())").stdout == "2,4,6\n"
    assert ausfuehren("python", "print(input()[::-1])", eingabe="abc\n").stdout == "cba\n"


@live
def test_live_fehler_im_code_ist_ein_ergebnis():
    ergebnis = ausfuehren("python", "raise SystemExit(3)")
    assert ergebnis.exit_code == 3
    assert "ZeroDivisionError" in ausfuehren("python", "1/0").stderr


@live
def test_live_abschottung():
    probe = (
        "import os, socket\n"
        "try:\n    socket.create_connection(('127.0.0.1', 8150), 2); print('NETZ')\n"
        "except OSError: print('kein netz')\n"
        "print(sorted(os.listdir('/')))\n"
        "print(os.path.exists('/mnt/c'), os.path.exists('/home'))\n"
        "print(sorted(os.environ))\n"
    )
    zeilen = ausfuehren("python", probe).stdout.splitlines()
    assert zeilen[0] == "kein netz"
    assert zeilen[1] == "['bin', 'dev', 'lib', 'proc', 'tmp', 'usr', 'work']"
    assert zeilen[2] == "False False"
    assert "HOME" not in zeilen[3] and "WSL" not in zeilen[3]


@live
def test_live_grenzen():
    assert ausfuehren("python", "while True: pass", zeit_s=1).zeit_ueberschritten
    assert ausfuehren("javascript", "for(;;){}", zeit_s=1).zeit_ueberschritten
    viel = ausfuehren("python", "print('x' * 5_000_000)")
    assert viel.abgeschnitten and len(viel.stdout) <= 20_000
    speicher = ausfuehren("python", "a = bytearray(2 * 1024**3); print('zu viel')")
    assert "zu viel" not in speicher.stdout and speicher.exit_code != 0
    voll = ausfuehren("python", "open('/work/gross', 'wb').write(b'x' * 64 * 1024**2); print('voll')")
    assert "voll" not in voll.stdout
    js = ausfuehren("javascript", "const a=[]; for(;;) a.push(new Array(1e6).fill(1)); ", zeit_s=10)
    assert js.exit_code != 0 or js.zeit_ueberschritten
    verschachtelt = ausfuehren("python", "import os\nos.unshare(os.CLONE_NEWUSER)\nprint('neu')")
    assert "neu" not in verschachtelt.stdout
    gabel = ausfuehren("python", "import os\nwhile True:\n    os.fork()", zeit_s=2)
    assert gabel.zeit_ueberschritten or gabel.exit_code != 0
    assert ausfuehren("python", "print('lebt')").stdout == "lebt\n"  # danach laeuft der Laeufer weiter
