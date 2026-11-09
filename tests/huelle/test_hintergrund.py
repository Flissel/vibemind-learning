"""Hintergrund-Spur: Vorrang, Doppelte, Fehler, Sperre."""

from __future__ import annotations

import threading
import time

from spaces.learning.huelle.hintergrund import Spur

WARTE = 2.0


def _warte_bis(bedingung: object, grenze: float = WARTE) -> bool:
    ende = time.monotonic() + grenze
    while time.monotonic() < ende:
        if callable(bedingung) and bedingung():
            return True
        time.sleep(0.01)
    return False


def test_vorrang_vor_reihenfolge() -> None:
    spur = Spur("t")
    freigabe = threading.Event()
    laeuft = threading.Event()
    reihenfolge: list[str] = []

    def blocker() -> None:
        laeuft.set()
        freigabe.wait(WARTE)

    assert spur.einreihen("blocker", blocker, vorrang=0)
    assert laeuft.wait(WARTE)
    assert spur.einreihen("a", lambda: reihenfolge.append("a"), vorrang=1)
    assert spur.einreihen("b", lambda: reihenfolge.append("b"), vorrang=0)
    assert spur.einreihen("c", lambda: reihenfolge.append("c"), vorrang=1)
    freigabe.set()
    assert _warte_bis(lambda: len(reihenfolge) == 3)
    assert reihenfolge == ["b", "a", "c"]
    spur.beenden(WARTE)


def test_doppelter_schluessel_abgelehnt() -> None:
    spur = Spur("t")
    freigabe = threading.Event()
    laeuft = threading.Event()

    def arbeit() -> None:
        laeuft.set()
        freigabe.wait(WARTE)

    assert spur.einreihen("k", arbeit)
    assert laeuft.wait(WARTE)
    assert spur.zustand("k") == "laeuft"
    assert spur.einreihen("k", lambda: None) is False
    assert spur.einreihen("w", lambda: None)
    assert spur.zustand("w") == "wartet"
    assert spur.einreihen("w", lambda: None) is False
    freigabe.set()
    assert _warte_bis(lambda: spur.zustand("k") is None and spur.zustand("w") is None)
    assert spur.einreihen("k", lambda: None) is True
    spur.beenden(WARTE)


def test_fehler_haelt_spur_nicht_an() -> None:
    spur = Spur("t")
    fertig = threading.Event()

    def kaputt() -> None:
        raise RuntimeError("geheim")

    assert spur.einreihen("1", kaputt)
    assert spur.einreihen("2", fertig.set)
    assert fertig.wait(WARTE)
    spur.beenden(WARTE)


def test_sperre_wird_gehalten() -> None:
    sperre = threading.Lock()
    spur = Spur("t", sperre=sperre)
    gesehen: list[bool] = []
    fertig = threading.Event()

    def arbeit() -> None:
        gesehen.append(sperre.locked())
        fertig.set()

    assert spur.einreihen("x", arbeit)
    assert fertig.wait(WARTE)
    assert gesehen == [True]
    assert _warte_bis(lambda: not sperre.locked())
    spur.beenden(WARTE)


def test_wartend_je_praefix() -> None:
    spur = Spur("t")
    freigabe = threading.Event()
    laeuft = threading.Event()

    def blocker() -> None:
        laeuft.set()
        freigabe.wait(WARTE)

    assert spur.einreihen("s1:1", blocker)
    assert laeuft.wait(WARTE)
    assert spur.einreihen("s1:2", lambda: None)
    assert spur.einreihen("s2:1", lambda: None)
    assert spur.wartend("s1:") == 2
    assert spur.wartend("s2:") == 1
    freigabe.set()
    spur.beenden(WARTE)


def test_nach_beenden_wird_nichts_angenommen() -> None:
    spur = Spur("t")
    spur.beenden(WARTE)
    assert spur.einreihen("x", lambda: None) is False
