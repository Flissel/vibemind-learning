"""Sperre: Zustaende, fail-closed, thread-sicher."""

from __future__ import annotations

import logging
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


def test_dauerhaft_gesperrt_wird_nie_frei() -> None:
    s = Sperre()
    s.frei()
    s.dauerhaft_gesperrt()
    assert s.gesperrt() is True
    s.frei()
    assert s.gesperrt() is True
    s.offen("a")
    s.unbekannt()
    s.frei()
    assert s.gesperrt() is True


def _paket_mit(tmp_path: Path, inhalt: str | None) -> Path:
    paket = tmp_path / "penecho"
    paket.mkdir()
    if inhalt is not None:
        (paket / "package.json").write_text(inhalt, encoding="utf-8")
    return paket


def test_gepruefte_penecho_version_laesst_sperre_beweglich(tmp_path: Path) -> None:
    from spaces.learning.huelle.sperre import GEPRUEFTE_PENECHO_VERSION, penecho_version_pruefen

    assert GEPRUEFTE_PENECHO_VERSION == "1.4.2"
    s = Sperre()
    paket = _paket_mit(tmp_path, '{"name": "penecho", "version": "1.4.2"}')
    assert penecho_version_pruefen(paket, s) is True
    s.frei()
    assert s.gesperrt() is False


@pytest.mark.parametrize("inhalt", [
    '{"name": "penecho", "version": "1.4.3"}', '{"name": "penecho", "version": "1.5.0"}',
    '{"name": "anders", "version": "1.4.2"}', '{"name": "penecho"}', "kein json", None,
])
def test_andere_penecho_version_sperrt_dauerhaft(tmp_path: Path, inhalt: str | None,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    from spaces.learning.huelle.sperre import penecho_version_pruefen

    caplog.set_level(logging.WARNING)
    s = Sperre()
    assert penecho_version_pruefen(_paket_mit(tmp_path, inhalt), s) is False
    s.frei()
    assert s.gesperrt() is True
    assert "PenEcho" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_build_apps_prueft_penecho_version(tmp_path: Path) -> None:
    import json

    from spaces.learning.huelle.einstellung import einstellung_lesen
    from spaces.learning.huelle.server import build_apps

    env = {
        "LEARNING_HUELLE_BASIS": "https://lernen.example.test",
        "LEARNING_HUELLE_FLAECHE_BASIS": "https://lernen.example.test:8443",
        "LEARNING_HUELLE_PENECHO_STATE": str(tmp_path / "state"),
        "LEARNHOUSE_LEARNING_SERVICE_KEY": "k" * 40,
        "LEARNING_HUELLE_ACTOR": "lh-1-test",
        "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": "11111111-1111-4111-8111-111111111111", "titel": "T"}]),
    }
    for version, frei_moeglich in (("1.4.2", True), ("9.9.9", False)):
        paket = tmp_path / version
        paket.mkdir()
        (paket / "package.json").write_text(json.dumps({"name": "penecho", "version": version}), encoding="utf-8")
        seiten, _ = build_apps(einstellung_lesen({**env, "LEARNING_HUELLE_PENECHO_PAKET": str(paket)}))
        sperre = seiten._api._sperre
        sperre.frei()
        assert sperre.gesperrt() is (not frei_moeglich)
