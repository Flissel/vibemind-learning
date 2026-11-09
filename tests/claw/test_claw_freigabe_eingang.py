"""Kursfreigaben im Freigabe-Eingang: Meldung beim Vorbereiten, Abholer im Claw-Prozess."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from spaces.learning.claw import freigabe_eingang, werkzeuge
from spaces.learning.claw.freigabe import FreigabeStore
from spaces.learning.claw.freigabe_eingang import FreigabeEingang
from spaces.learning.claw.learning_client import LearningClient

KEY = "k" * 40
API = "http://127.0.0.1:8090/api/v1"
MCP = "http://127.0.0.1:8091/mcp"
COURSE = "kurs-1"
FREIGABE = "f1f1f1f1-0000-4000-8000-000000000001"
EINGANG = "https://eingang.example"


class FakeEingang:
    """Zeichnet jeden Rumpf auf, den der echte Client senden wuerde."""

    def __init__(self, auftraege: list[dict[str, Any]] | None = None,
                 offene: list[dict[str, Any]] | None = None) -> None:
        self.eintraege: list[tuple[Any, ...]] = []
        self.geschlossen: list[tuple[str, str]] = []
        self.ergebnisse: list[tuple[str, bool, str | None]] = []
        self._auftraege = auftraege or []
        self._offene = offene or []

    def eintragen(self, art, quelle_ref, titel, link=None, details=None):  # type: ignore[no-untyped-def]
        self.eintraege.append((art, quelle_ref, titel, link, details))
        return "fg-aaaaaaaaaa"

    def schliessen(self, quelle_ref, status="erledigt"):  # type: ignore[no-untyped-def]
        self.geschlossen.append((quelle_ref, status))
        return 1

    def offene(self):  # type: ignore[no-untyped-def]
        return self._offene

    def auftraege(self):  # type: ignore[no-untyped-def]
        return self._auftraege

    def ergebnis(self, auftrag_id, ok, fehler=None):  # type: ignore[no-untyped-def]
        self.ergebnisse.append((auftrag_id, ok, fehler))
        return True


def _vorbereitet(blockiert: bool = False, nichts: bool = False, titel: str = "PowerShell Grundlagen") -> dict[str, Any]:
    return {
        "freigabe_id": FREIGABE, "nummer": 3, "titel": titel, "learnhouse_revision": 7,
        "blockiert": blockiert, "nichts_zu_veroeffentlichen": nichts,
        "vergleich": {"neu": [], "geaendert": [], "textkorrektur": [], "geloescht": [], "unveraendert": 2},
        "befunde": [],
    }


class Api:
    def __init__(self) -> None:
        self.vorbereitet = _vorbereitet()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/api/v1/werkstatt/kurse/{COURSE}/freigabe":
            return httpx.Response(201, json=self.vorbereitet)
        return httpx.Response(404, json={"code": "not_found", "message": "x"})


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Api]:
    a = Api()
    monkeypatch.setattr(
        werkzeuge, "_client", lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(a.handle))
    )
    monkeypatch.setattr(werkzeuge, "_freigaben", FreigabeStore())
    monkeypatch.delenv("LEARNING_CLAW_FREIGABE_BASIS", raising=False)
    monkeypatch.setattr(werkzeuge, "_browser_oeffnen", lambda url: True)
    yield a


@pytest.fixture
def eingang(monkeypatch: pytest.MonkeyPatch) -> FakeEingang:
    fake = FakeEingang()
    monkeypatch.setattr(werkzeuge, "_eingang", lambda: fake)
    monkeypatch.setattr(werkzeuge, "_melde_runner", lambda arbeit: arbeit())  # synchron
    return fake


def test_anzeigen_meldet_kurs(api: Api, eingang: FakeEingang) -> None:
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert out["ok"] is True and out["daten"]["nummer"] == 3
    assert eingang.eintraege == [(
        "kursfreigabe", f"kurs:{COURSE}", "Kurs „PowerShell Grundlagen“ freigeben", None,
        {"course_id": COURSE, "nummer": 3},
    )]


def test_titel_ohne_at_und_gekappt(api: Api, eingang: FakeEingang) -> None:
    api.vorbereitet = _vorbereitet(titel="Mail @ team " + "x" * 400)
    werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    titel = eingang.eintraege[0][2]
    assert "@" not in titel and len(titel) <= 200


@pytest.mark.parametrize("zustand", [{"blockiert": True}, {"nichts": True}])
def test_blockiert_meldet_nicht(api: Api, eingang: FakeEingang, zustand: dict[str, bool]) -> None:
    api.vorbereitet = _vorbereitet(**zustand)
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert out["ok"] is True
    assert eingang.eintraege == []


def test_meldefehler_aendert_rueckgabe_nicht(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    class Kaputt(FakeEingang):
        def eintragen(self, *a: Any, **k: Any) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(werkzeuge, "_eingang", lambda: Kaputt())
    monkeypatch.setattr(werkzeuge, "_melde_runner", lambda arbeit: arbeit())
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert out["ok"] is True and out["daten"]["nummer"] == 3


def test_meldung_laeuft_in_eigenem_faden_ohne_zu_blockieren(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    frei = threading.Event()
    lief_in: list[threading.Thread] = []

    class Haengt(FakeEingang):
        def eintragen(self, *a: Any, **k: Any) -> str:
            lief_in.append(threading.current_thread())
            frei.wait(5)  # toter Eingang
            return "fg-aaaaaaaaaa"

    monkeypatch.setattr(werkzeuge, "_eingang", lambda: Haengt())
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)  # kehrt zurueck, obwohl der Eingang haengt
    try:
        assert out["ok"] is True and out["daten"]["nummer"] == 3
        for _ in range(100):
            if lief_in:
                break
            threading.Event().wait(0.02)
        assert lief_in and lief_in[0] is not threading.main_thread() and lief_in[0].daemon
    finally:
        frei.set()


def test_auftrag_ruft_anzeigen_im_prozess() -> None:
    fake = FakeEingang(auftraege=[
        {"id": "a1", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": COURSE}},
        {"id": "a2", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": "../x"}},
        {"id": "a6", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": "kurs\n"}},
        {"id": "a7", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": "kürs"}},
        {"id": "a3", "art": "kurs_freigabe_zeigen", "nutzlast": {}},
        {"id": "a4", "art": "fremd", "nutzlast": {"course_id": COURSE}},
        {"id": "a5", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": "wirft"}},
    ])
    gerufen: list[str] = []

    def anzeigen(course_id: str) -> dict[str, Any]:
        gerufen.append(course_id)
        if course_id == "wirft":
            raise PermissionError("geheime Meldung /freigabe/abc")
        return {"ok": True, "daten": {"link_geoeffnet": True}}

    assert freigabe_eingang.abholer_einmal(fake, anzeigen=anzeigen, freigaben_lesen=lambda c: []) == 7
    assert gerufen == [COURSE, "wirft"]
    assert fake.ergebnisse == [
        ("a1", True, None), ("a2", False, "ungueltig"), ("a6", False, "ungueltig"),
        ("a7", False, "ungueltig"), ("a3", False, "ungueltig"),
        ("a4", False, "unbekannte_art"), ("a5", False, "PermissionError"),
    ]


def test_anzeigen_ok_false_wird_fehler() -> None:
    fake = FakeEingang(auftraege=[{"id": "a1", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": COURSE}}])
    freigabe_eingang.abholer_einmal(
        fake, anzeigen=lambda c: {"ok": False, "fehler": "Text"}, freigaben_lesen=lambda c: []
    )
    assert fake.ergebnisse == [("a1", False, "anzeigen_fehlgeschlagen")]


@pytest.mark.parametrize("daten", [
    {"link_geoeffnet": False, "handy_link_gesendet": False}, {}, None,
])
def test_ohne_link_kein_erfolg(daten: dict[str, Any] | None) -> None:
    fake = FakeEingang(auftraege=[{"id": "a1", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": COURSE}}])
    freigabe_eingang.abholer_einmal(
        fake, anzeigen=lambda c: {"ok": True, "daten": daten}, freigaben_lesen=lambda c: []
    )
    assert fake.ergebnisse == [("a1", False, "kein_link")]


def test_handy_link_allein_genuegt() -> None:
    fake = FakeEingang(auftraege=[{"id": "a1", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": COURSE}}])
    freigabe_eingang.abholer_einmal(
        fake, anzeigen=lambda c: {"ok": True, "daten": {"handy_link_gesendet": True}}, freigaben_lesen=lambda c: []
    )
    assert fake.ergebnisse == [("a1", True, None)]


def test_freigegeben_schliesst() -> None:
    fake = FakeEingang(offene=[
        {"kennung": "fg-1", "quelle_ref": "kurs:fertig"},
        {"kennung": "fg-2", "quelle_ref": "kurs:offen"},
        {"kennung": "fg-3", "quelle_ref": "kurs:"},
        {"kennung": "fg-4", "quelle_ref": "pr:1"},
        {"kennung": "fg-5", "quelle_ref": "kurs:a/b"},
        {"kennung": "fg-6", "quelle_ref": "kurs:kaputt"},
        {"kennung": "fg-7", "quelle_ref": "kurs:fertig\n"},
        {"kennung": "fg-8", "quelle_ref": "kurs:fertigä"},
    ])
    zustand = {"fertig": "freigegeben", "offen": "angefordert"}

    def lesen(course_id: str) -> list[dict[str, Any]]:
        if course_id == "kaputt":
            raise ValueError("x")
        return [{"nummer": 2, "zustand": zustand[course_id]}, {"nummer": 1, "zustand": "freigegeben"}]

    assert freigabe_eingang.abholer_einmal(fake, anzeigen=lambda c: {}, freigaben_lesen=lesen) == 1
    assert fake.geschlossen == [("kurs:fertig", "erledigt")]


def test_freigaben_standard_liest_ueber_learningclient(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    pfade: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        pfade.append(request.url.path)
        return httpx.Response(200, json={"freigaben": [{"nummer": 2, "zustand": "freigegeben"}]})

    monkeypatch.setattr(
        werkzeuge, "_client", lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(handle))
    )
    assert freigabe_eingang._freigaben_standard(COURSE)[0]["zustand"] == "freigegeben"
    assert pfade == [f"/api/v1/werkstatt/kurse/{COURSE}/freigaben"]


def test_geheimnis_nie_gesendet(api: Api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    opened: list[str] = []
    monkeypatch.setattr(werkzeuge, "_browser_oeffnen", lambda url: opened.append(url) or True)
    fake = FakeEingang(auftraege=[
        {"id": "a1", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": COURSE}},
        {"id": "a2", "art": "kurs_freigabe_zeigen", "nutzlast": {"course_id": COURSE}},
    ], offene=[{"kennung": "fg-1", "quelle_ref": f"kurs:{COURSE}"}])
    monkeypatch.setattr(werkzeuge, "_eingang", lambda: fake)
    monkeypatch.setattr(werkzeuge, "_melde_runner", lambda arbeit: arbeit())
    with caplog.at_level(logging.DEBUG):
        freigabe_eingang.abholer_einmal(fake, freigaben_lesen=lambda c: [{"zustand": "angefordert"}])
        freigabe_eingang.abholer_einmal(fake, freigaben_lesen=lambda c: [{"zustand": "freigegeben"}])
    assert len(opened) == 4
    geheimnisse = [u.rsplit("/", 1)[1] for u in opened]
    gesendet = json.dumps([fake.eintraege, fake.geschlossen, fake.ergebnisse], default=str)
    assert fake.eintraege and fake.ergebnisse
    assert "/freigabe/" not in gesendet and "/freigabe/" not in caplog.text
    for url, geheimnis in zip(opened, geheimnisse):
        assert geheimnis and geheimnis not in gesendet and geheimnis not in caplog.text
        assert url not in gesendet and url not in caplog.text


def test_client_sendet_keinen_freigabe_link(tmp_path: Path) -> None:
    datei = tmp_path / "schluessel"
    datei.write_text("geheim\r\n", encoding="utf-8")
    aufrufe: list[Any] = []

    def oeffnen(req: Any, timeout: float) -> Any:
        aufrufe.append(req)
        raise OSError("aus")

    client = FreigabeEingang(EINGANG, datei, oeffnen=oeffnen)
    assert client.eintragen("kursfreigabe", "kurs:x", "t", None, {"u": "https://h.example/freigabe/zzz"}) is None
    assert aufrufe == []
    assert client.eintragen("kursfreigabe", "kurs:x", "t") is None  # Netz aus: kein Wurf
    assert aufrufe[0].get_header("Authorization") == "Bearer geheim"


def test_ohne_umgebung_aus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FREIGABE_URL", raising=False)
    monkeypatch.delenv("FREIGABE_SCHLUESSEL_DATEI", raising=False)
    assert freigabe_eingang.aus_umgebung() is None
    assert freigabe_eingang.starte_abholer() is None
    monkeypatch.setenv("FREIGABE_URL", EINGANG)
    assert freigabe_eingang.aus_umgebung() is None
    monkeypatch.setenv("FREIGABE_SCHLUESSEL_DATEI", str(tmp_path / "fehlt"))
    assert freigabe_eingang.aus_umgebung() is None
    datei = tmp_path / "k"
    datei.write_text("x", encoding="utf-8")
    monkeypatch.setenv("FREIGABE_SCHLUESSEL_DATEI", str(datei))
    assert isinstance(freigabe_eingang.aus_umgebung(), FreigabeEingang)


def test_schleife_ueberlebt_fehler_und_stoppt() -> None:
    stop = threading.Event()
    zaehler = {"n": 0}

    class Client(FakeEingang):
        def auftraege(self):  # type: ignore[no-untyped-def]
            zaehler["n"] += 1
            if zaehler["n"] == 1:
                raise RuntimeError("einmal kaputt")
            if zaehler["n"] >= 3:
                stop.set()
            return []

    freigabe_eingang.abholer_schleife(Client(), stop, takt_s=0.0)
    assert zaehler["n"] == 3


def test_starte_abholer_ist_daemon() -> None:
    stop = threading.Event()
    faden = freigabe_eingang.starte_abholer(FakeEingang(), stop)  # type: ignore[arg-type]
    try:
        assert faden is not None and faden.daemon
    finally:
        stop.set()
        faden.join(2)  # type: ignore[union-attr]


def test_schleife_wartet_vor_dem_ersten_durchlauf() -> None:
    stop = threading.Event()
    stop.set()  # sofort gestoppt: kein Durchlauf, nicht einmal der erste

    class Client(FakeEingang):
        def auftraege(self):  # type: ignore[no-untyped-def]
            raise AssertionError("darf nicht laufen")

    freigabe_eingang.abholer_schleife(Client(), stop, takt_s=0.0)


def test_umgebungsschluessel_werden_aus_env_datei_geladen() -> None:
    from spaces.learning.claw import server

    assert {"FREIGABE_URL", "FREIGABE_SCHLUESSEL_DATEI"} <= set(server._ENV_KEYS)
