"""Zugangstoken, Einstellung und Link-Versand der Huelle."""

from __future__ import annotations

import json

import pytest

from spaces.learning.huelle import link
from spaces.learning.huelle.einstellung import einstellung_lesen
from spaces.learning.huelle.zugang import GUELTIG_SEKUNDEN, token_erzeugen, token_gueltig

SCHLUESSEL = "k" * 40
KURS = "11111111-1111-4111-8111-111111111111"


def _env(**extra: str) -> dict[str, str]:
    env = {
        "LEARNING_HUELLE_BASIS": "https://lernen.example.test",
        "LEARNING_HUELLE_PENECHO_STATE": "/tmp/state",
        "LEARNHOUSE_LEARNING_SERVICE_KEY": SCHLUESSEL,
        "LEARNING_HUELLE_ACTOR": "lh-1-test",
        "LEARNING_HUELLE_KURSE": json.dumps([{"course_id": KURS, "titel": "Testkurs"}]),
    }
    env.update(extra)
    return env


def test_token_gueltig_abgelaufen_manipuliert() -> None:
    token = token_erzeugen(SCHLUESSEL, jetzt=1000.0)
    assert token_gueltig(SCHLUESSEL, token, jetzt=1000.0 + 10)
    assert not token_gueltig(SCHLUESSEL, token, jetzt=1000.0 + GUELTIG_SEKUNDEN + 1)
    assert not token_gueltig("x" * 40, token, jetzt=1000.0 + 10)
    assert not token_gueltig(SCHLUESSEL, token[:-2] + "AA", jetzt=1000.0 + 10)
    assert not token_gueltig(SCHLUESSEL, "kein token")
    assert not token_gueltig(SCHLUESSEL, "")


def test_einstellung_vorgaben() -> None:
    e = einstellung_lesen(_env())
    assert (e.host, e.port) == ("127.0.0.1", 8150)
    assert e.basis == "https://lernen.example.test"
    assert e.erlaubte_hosts == {"lernen.example.test", "127.0.0.1:8150"}
    assert e.kurse[0].course_id == KURS


def test_einstellung_meldet_fehlenden_wert_ohne_inhalt() -> None:
    env = _env()
    del env["LEARNHOUSE_LEARNING_SERVICE_KEY"]
    with pytest.raises(RuntimeError) as fehler:
        einstellung_lesen(env)
    assert "LEARNHOUSE_LEARNING_SERVICE_KEY" in str(fehler.value)
    assert SCHLUESSEL not in str(fehler.value)


@pytest.mark.parametrize(
    ("name", "wert"),
    [
        ("LEARNHOUSE_LEARNING_SERVICE_KEY", "k" * 31),
        ("LEARNING_HUELLE_ACTOR", "../boese"),
        ("LEARNING_HUELLE_BASIS", "http://lernen.example.test"),
        ("LEARNING_HUELLE_KURSE", "[]"),
        ("LEARNING_HUELLE_KURSE", '[{"course_id": "kein-uuid", "titel": "x"}]'),
    ],
)
def test_einstellung_lehnt_ungueltige_werte_ab(name: str, wert: str) -> None:
    with pytest.raises(RuntimeError) as fehler:
        einstellung_lesen(_env(**{name: wert}))
    assert name in str(fehler.value)
    assert wert not in str(fehler.value) or len(wert) < 8


def test_link_sendet_und_gibt_nichts_aus(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from spaces.learning.claw import werkzeuge

    for name, wert in _env().items():
        monkeypatch.setenv(name, wert)
    gesendet: list[str] = []
    monkeypatch.setattr(werkzeuge, "_telegram_senden", lambda text: gesendet.append(text) or True)
    assert link.main() == 0
    ausgabe = capsys.readouterr().out
    assert ausgabe.strip() == "gesendet"
    assert gesendet[0].count("https://lernen.example.test/lernen/zugang/") == 1

    monkeypatch.setattr(werkzeuge, "_telegram_senden", lambda text: False)
    assert link.main() == 1
    assert capsys.readouterr().out.strip() == "nicht gesendet"
