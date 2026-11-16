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
    assert lauf.pruefen({"sprache": "python", "quelltext": "x", "eingabe": "", "zeit_s": 2}) == lauf.Auftrag(
        "python", "x", "", 2)


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
    js = ausfuehren("javascript", "const a=[]; for(;;) a.push(new Array(1e6).fill(1)); ", zeit_s=10,
                    erlaubnisse=("lange_laufzeit",))  # mehr als 5 s nur noch mit dieser Erlaubnis
    assert js.exit_code != 0 or js.zeit_ueberschritten
    verschachtelt = ausfuehren("python", "import os\nos.unshare(os.CLONE_NEWUSER)\nprint('neu')")
    assert "neu" not in verschachtelt.stdout
    gabel = ausfuehren("python", "import os\nwhile True:\n    os.fork()", zeit_s=2)
    assert gabel.zeit_ueberschritten or gabel.exit_code != 0
    assert ausfuehren("python", "print('lebt')").stdout == "lebt\n"  # danach laeuft der Laeufer weiter


# ---- Erlaubnisse: Pakete, Diagramm, laengere Laufzeit, Uebungsdaten ---------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"bild"
SITE = ("/usr/lib/python3.14/site-packages",)


def test_ohne_erlaubnis_bleibt_der_auftrag_wie_bisher_und_pakete_sind_ausgeblendet():
    starter = Starter(_antwort())
    ausfuehren("python", "print(1)", starter=starter)
    assert json.loads(starter.aufrufe[0][1]["input"]) == {"sprache": "python", "quelltext": "print(1)",
                                                         "eingabe": "", "zeit_s": 5}
    befehl = lauf.befehl("python", "/tmp/x/main.py", paketordner=SITE)
    text = " ".join(befehl)
    # Ein leeres tmpfs liegt ueber dem Paketordner, und zwar NACH dem Einbinden von /usr.
    assert "--tmpfs /usr/lib/python3.14/site-packages" in text
    assert text.index("--ro-bind /usr /usr") < text.index("--tmpfs /usr/lib/python3.14/site-packages")
    assert "MPLBACKEND" not in text and "aufseher" not in text and "/work/daten" not in text


def test_pakete_nur_mit_erlaubnis_sichtbar():
    befehl = lauf.befehl("python", "/tmp/x/main.py", erlaubnisse=("pakete",), paketordner=SITE)
    text = " ".join(befehl)
    assert "--tmpfs /usr/lib/python3.14/site-packages" not in text
    assert "--setenv MPLBACKEND Agg" in text and "--setenv MPLCONFIGDIR /tmp" in text
    assert "--setenv OPENBLAS_NUM_THREADS 1" in text
    assert "--unshare-all" in befehl and "--share-net" not in befehl  # Netz bleibt immer zu


def test_diagramm_laeuft_ueber_den_aufseher_mit_eigenem_kanal():
    befehl = lauf.befehl("python", "/tmp/x/main.py", erlaubnisse=("diagramm",), paketordner=SITE, bildkanal=7)
    assert befehl[-5:] == ["python3", lauf.AUFSEHER, "7", "python3", "main.py"]
    # Ohne die Erlaubnis gibt es keinen Kanal, auch wenn einer angegeben waere.
    ohne = lauf.befehl("python", "/tmp/x/main.py", paketordner=SITE, bildkanal=7)
    assert ohne[-2:] == ["python3", "main.py"]


def test_uebungsdaten_liegen_nur_lesend_im_lauf():
    befehl = lauf.befehl("python", "/tmp/x/main.py", erlaubnisse=("daten",), paketordner=SITE,
                         datenordner="/tmp/x/daten")
    assert "--ro-bind /tmp/x/daten /work/daten" in " ".join(befehl)


@pytest.mark.parametrize("auftrag", [
    {"zeit_s": 6},                                               # laenger als 5 s nur mit Erlaubnis
    {"erlaubnisse": ["netz"]},                                   # unbekannte Erlaubnis
    {"erlaubnisse": ["pakete", "pakete"]},                       # doppelt
    {"erlaubnisse": "pakete"},                                   # keine Liste
    {"dateien": [{"name": "a.csv", "inhalt": "1"}]},             # Dateien nur mit "daten"
    {"erlaubnisse": ["daten"], "dateien": [{"name": "../a", "inhalt": "1"}]},
    {"erlaubnisse": ["daten"], "dateien": [{"name": ".versteckt", "inhalt": "1"}]},
    {"erlaubnisse": ["daten"], "dateien": [{"name": "a/b.csv", "inhalt": "1"}]},
    {"erlaubnisse": ["daten"], "dateien": [{"name": "a.csv", "inhalt": "1"}, {"name": "a.csv", "inhalt": "2"}]},
    {"erlaubnisse": ["daten"], "dateien": [{"name": f"d{i}.csv", "inhalt": "1"} for i in range(6)]},
    {"erlaubnisse": ["daten"], "dateien": [{"name": "a.csv", "inhalt": "x" * 200_001}]},
    {"erlaubnisse": ["daten"], "dateien": [{"name": "a.csv", "inhalt": 5}]},
])
def test_lauf_py_lehnt_auftraege_ausserhalb_der_erlaubnisse_ab(auftrag):
    basis = {"sprache": "python", "quelltext": "x", "eingabe": "", "zeit_s": 2}
    with pytest.raises(ValueError):
        lauf.pruefen({**basis, **auftrag})


def test_lauf_py_nimmt_erlaubte_auftraege_an():
    a = lauf.pruefen({"sprache": "python", "quelltext": "x", "eingabe": "", "zeit_s": 20,
                      "erlaubnisse": ["lange_laufzeit", "daten"],
                      "dateien": [{"name": "umsatz.csv", "inhalt": "a;b\n1;2\n"}]})
    assert a.zeit_s == 20 and a.erlaubnisse == ("lange_laufzeit", "daten")
    assert a.dateien == (("umsatz.csv", "a;b\n1;2\n"),)
    ohne = lauf.pruefen({"sprache": "python", "quelltext": "x", "eingabe": "", "zeit_s": 5})
    assert ohne.erlaubnisse == () and ohne.dateien == ()


def test_host_reicht_erlaubnisse_und_dateien_durch_und_liest_bilder():
    import base64

    starter = Starter(_antwort(bilder=[{"name": "kurve.png", "png_base64": base64.b64encode(PNG).decode()}]))
    ergebnis = ausfuehren("python", "x", zeit_s=20, erlaubnisse=("pakete", "diagramm", "lange_laufzeit", "daten"),
                          dateien=(("umsatz.csv", "1;2\n"),), starter=starter)
    auftrag = json.loads(starter.aufrufe[0][1]["input"])
    assert auftrag["erlaubnisse"] == ["pakete", "diagramm", "lange_laufzeit", "daten"]
    assert auftrag["dateien"] == [{"name": "umsatz.csv", "inhalt": "1;2\n"}]
    assert [(b.name, b.png) for b in ergebnis.bilder] == [("kurve.png", PNG)]
    assert starter.aufrufe[0][1]["timeout"] > 20


def test_host_verwirft_bilder_die_keine_png_sind_oder_zu_viele():
    import base64

    b64 = lambda roh: base64.b64encode(roh).decode()  # noqa: E731
    bilder = [{"name": "a.png", "png_base64": b64(b"kein bild")}, {"name": "../b.png", "png_base64": b64(PNG)},
              {"name": "c.png", "png_base64": "%%%"}, *({"name": f"g{i}.png", "png_base64": b64(PNG)} for i in range(5))]
    ergebnis = ausfuehren("python", "x", erlaubnisse=("diagramm",), starter=Starter(_antwort(bilder=bilder)))
    assert [b.name for b in ergebnis.bilder] == ["g0.png", "g1.png", "g2.png"]
    # Ohne die Erlaubnis zaehlen mitgelieferte Bilder nicht.
    assert ausfuehren("python", "x", starter=Starter(_antwort(bilder=bilder))).bilder == ()


def test_host_laesst_lange_laufzeit_nur_mit_erlaubnis_zu():
    starter = Starter(_antwort())
    with pytest.raises(UngueltigerAuftrag):
        ausfuehren("python", "x", zeit_s=6, starter=starter)
    with pytest.raises(UngueltigerAuftrag):
        ausfuehren("python", "x", erlaubnisse=("netz",), starter=starter)
    assert starter.aufrufe == []


def test_aufseher_sammelt_nur_echte_png_dateien(tmp_path):
    from spaces.learning.huelle.laeufer import aufseher

    ordner = tmp_path / "ausgabe"
    ordner.mkdir()
    (ordner / "b.png").write_bytes(PNG + b"2")
    (ordner / "a.png").write_bytes(PNG + b"1")
    (ordner / "text.txt").write_bytes(PNG)                      # falsche Endung
    (ordner / "falsch.png").write_bytes(b"kein bild")           # keine PNG-Kennung
    (ordner / "gross.png").write_bytes(PNG + b"x" * aufseher.BILD_BYTES)
    (ordner / "unter").mkdir()
    for i in range(4):
        (ordner / f"z{i}.png").write_bytes(PNG)
    gesammelt = aufseher.bilder_sammeln(str(ordner))
    assert [name for name, _ in gesammelt] == ["a.png", "b.png", "z0.png"]
    assert gesammelt[0][1] == PNG + b"1"
    assert aufseher.bilder_sammeln(str(tmp_path / "fehlt")) == []


@live
def test_live_pakete_nur_mit_erlaubnis():
    ohne = ausfuehren("python", "import numpy")
    assert ohne.exit_code != 0 and "ModuleNotFoundError" in ohne.stderr
    # Auch nicht ueber einen selbst ergaenzten Suchpfad: der Ordner ist im Lauf leer.
    trick = ausfuehren("python", "import sys, os\nsys.path.append('/usr/lib/python3.14/site-packages')\n"
                                 "print(os.listdir('/usr/lib/python3.14/site-packages'))\nimport numpy")
    assert trick.stdout.strip() == "[]" and trick.exit_code != 0
    mit = ausfuehren("python", "import numpy as np\nprint(int(np.arange(5).sum()))", erlaubnisse=("pakete",))
    assert mit.stdout == "10\n"
    # Das Netz bleibt auch mit Paketen zu.
    netz = ausfuehren("python", "import socket\ntry:\n    socket.create_connection(('127.0.0.1', 8150), 2); print('NETZ')\n"
                                "except OSError: print('kein netz')", erlaubnisse=("pakete",))
    assert netz.stdout == "kein netz\n"


@live
def test_live_diagramm_kommt_als_bild_heraus():
    code = ("import numpy as np\nimport matplotlib.pyplot as plt\nx = np.linspace(0, 6.28, 100)\n"
            "plt.plot(x, np.sin(x))\nplt.savefig('ausgabe/kurve.png', dpi=60)\nprint('fertig')")
    ergebnis = ausfuehren("python", code, erlaubnisse=("pakete", "diagramm"))
    assert ergebnis.stdout == "fertig\n" and ergebnis.exit_code == 0
    assert [b.name for b in ergebnis.bilder] == ["kurve.png"]
    assert ergebnis.bilder[0].png.startswith(b"\x89PNG") and len(ergebnis.bilder[0].png) > 1000
    # Ohne die Erlaubnis `diagramm` kommt kein Bild heraus, auch wenn der Code eines schreibt.
    ohne = ausfuehren("python", "import os\nos.makedirs('ausgabe')\nopen('ausgabe/a.png','wb').write(b'\\x89PNG\\r\\n\\x1a\\nx')",
                      erlaubnisse=("pakete",))
    assert ohne.bilder == ()
    # Verweise und falsche Dateien bleiben drin; hoechstens drei Bilder.
    viele = ("import os\nos.makedirs('ausgabe', exist_ok=True)\nk = b'\\x89PNG\\r\\n\\x1a\\n'\n"
             "for i in range(5): open(f'ausgabe/b{i}.png', 'wb').write(k + b'x')\n"
             "os.symlink('/usr/bin/python3', 'ausgabe/a.png')\nopen('ausgabe/c.png', 'wb').write(b'kein bild')")
    assert [b.name for b in ausfuehren("python", viele, erlaubnisse=("diagramm",)).bilder] == ["b0.png", "b1.png", "b2.png"]
    # Fehler im Programm bleiben ein Ergebnis, die Ausgabe geht weiter durch den Aufseher.
    fehler = ausfuehren("python", "print('vorher')\nraise SystemExit(3)", erlaubnisse=("diagramm",))
    assert fehler.exit_code == 3 and fehler.stdout == "vorher\n" and fehler.bilder == ()


@live
def test_live_uebungsdaten_und_lange_laufzeit():
    lesen = ausfuehren("python", "print(open('daten/umsatz.csv').read().strip())\n"
                                 "try:\n    open('daten/neu.txt', 'w')\nexcept OSError: print('nur lesend')",
                       erlaubnisse=("daten",), dateien=(("umsatz.csv", "a;b\n1;2\n"),))
    assert lesen.stdout == "a;b\n1;2\nnur lesend\n"
    lang = ausfuehren("python", "import time\ntime.sleep(6)\nprint('durch')", zeit_s=10, erlaubnisse=("lange_laufzeit",))
    assert lang.stdout == "durch\n"
    assert ausfuehren("python", "import time\ntime.sleep(6)", zeit_s=5).zeit_ueberschritten
