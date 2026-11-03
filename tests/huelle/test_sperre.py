"""Sperre: Zustaende, fail-closed, thread-sicher."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest

from spaces.learning.huelle.sperre import KI_PFADE, Sperre


def test_sperre_startet_unbekannt_und_gesperrt() -> None:
    assert Sperre().gesperrt() is True


def test_sperre_frei_nach_frei() -> None:
    s = Sperre()
    s.offen("aufgabe-1")
    assert s.gesperrt() is True
    s.frei()
    assert s.gesperrt() is False
    s.unbekannt()
    assert s.gesperrt() is True


def test_ki_pfade_enthalten_startwerte() -> None:
    for pfad in ("/api/ai/command", "/api/suggest", "/api/plugins/improve"):
        assert pfad in KI_PFADE


def test_sperre_thread_sicher() -> None:
    s = Sperre()

    def wechsel() -> None:
        for i in range(500):
            s.offen(f"a{i}")
            s.gesperrt()
            s.frei()
            s.unbekannt()

    threads = [threading.Thread(target=wechsel) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.gesperrt() is True


def _penecho_paket() -> Path:
    roh = os.environ.get("LEARNING_HUELLE_PENECHO_PAKET", "").strip()
    if roh:
        return Path(roh)
    return Path(os.environ.get("LOCALAPPDATA", "")) / "VibeMind" / "penecho-test" / "node_modules" / "penecho"


def test_ki_pfade_decken_penechos_lokale_ki_routen() -> None:
    """PenEcho fuehrt in LOCAL_AI_ROUTES selbst, welche Routen ein Modell rufen; jede muss gesperrt sein."""
    from spaces.learning.huelle.durchreicher import ist_ki_pfad

    datei = _penecho_paket() / "src" / "server" / "remote-canvas-http.js"
    if not datei.is_file():
        pytest.skip("PenEcho ist hier nicht installiert")
    treffer = re.search(r"LOCAL_AI_ROUTES\s*=\s*new Set\(\[([^\]]*)\]\)", datei.read_text(encoding="utf-8"))
    assert treffer is not None
    routen = re.findall(r"\"([^\"]+)\"", treffer.group(1))
    assert routen
    for route in routen:
        assert ist_ki_pfad(route), route
