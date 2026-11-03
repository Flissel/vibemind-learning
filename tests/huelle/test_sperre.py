"""Sperre: Zustaende, fail-closed, thread-sicher."""

from __future__ import annotations

import threading

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
