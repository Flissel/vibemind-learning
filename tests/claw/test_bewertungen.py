"""Kursbewertungen im Claw: Telegram-Melder und signierte Uebersichtsseite."""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from spaces.learning.claw import bewertungen, werkzeuge
from spaces.learning.claw.bewertungen import melde_runde, nachricht, token_erzeugen
from spaces.learning.claw.bewertungen_seite import BewertungenSeite
from spaces.learning.claw.freigabe_seite import FreigabeSeite
from spaces.learning.claw.server import BearerAuth, FreigabeWeiche, build_app

PORT = 8139
ORIGIN = f"http://127.0.0.1:{PORT}"
SCHLUESSEL = "k" * 40
VERTEILUNG = {"gut": 3, "mittel": 1, "schlecht": 1}


@pytest.fixture(autouse=True)
def schluessel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", SCHLUESSEL)


def _b(**extra) -> dict:
    return {"id": "b1", "course_id": "c1", "kurs_titel": "Zwingen", "smiley": "gut",
            "stichworte": ["klar", "kurz"], "geaendert": False, "verteilung": VERTEILUNG, **extra}


def test_nachricht_format() -> None:
    assert nachricht(_b(), "https://x/bewertungen/t") == (
        "Kursbewertung · Zwingen\n"
        "😃 – „klar“, „kurz“\n"
        "Bisher 5 Bewertungen: 3 😃 · 1 🙂 · 1 😕\n"
        "Übersicht: https://x/bewertungen/t"
    )
    assert nachricht(_b(smiley="schlecht", stichworte=[], geaendert=True), "L") == (
        "Kursbewertung · Zwingen (geändert)\n"
        "😕 – ohne Stichworte\n"
        "Bisher 5 Bewertungen: 3 😃 · 1 🙂 · 1 😕\n"
        "Übersicht: L"
    )
    assert nachricht(_b(smiley="mittel", stichworte=["a"]), "L").splitlines()[1] == "🙂 – „a“"


class _Client:
    def __init__(self, neu=None, kurse=None, fehler=False) -> None:
        self.neu, self.kurse, self.fehler = neu or [], kurse or [], fehler
        self.gemeldet: list[list[str]] = []

    def get(self, path: str, *, roles: str) -> dict:
        if self.fehler:
            raise RuntimeError("down")
        if path == "/kursbewertungen/neu":
            return {"bewertungen": self.neu}
        assert path == "/kursbewertungen/uebersicht"
        return {"kurse": self.kurse}

    def post(self, path: str, body: dict, *, roles: str) -> dict:
        assert path == "/kursbewertungen/gemeldet"
        self.gemeldet.append(body["ids"])
        return {"markiert": len(body["ids"])}


def test_melde_runde_markiert_nur_gesendete() -> None:
    client = _Client(neu=[_b(id="b1"), _b(id="b2")])
    gesendet: list[str] = []

    def senden(text: str) -> bool:
        gesendet.append(text)
        return len(gesendet) == 1

    assert melde_runde(client, senden) == 1
    assert client.gemeldet == [["b1"]]
    assert len(gesendet) == 2


def test_melde_runde_ohne_telegram_bleibt_ungemeldet() -> None:
    client = _Client(neu=[_b()])

    def kaputt(text: str) -> bool:
        raise OSError("kein Skript")

    assert melde_runde(client, kaputt) == 0
    assert client.gemeldet == []


def test_melde_runde_ueberlebt_fehler() -> None:
    assert melde_runde(_Client(fehler=True), lambda t: True) == 0


def _seite(pfad: str):
    app = FreigabeWeiche(
        BearerAuth(None, "t" * 40), FreigabeSeite(PORT), None, None, BewertungenSeite(PORT)
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as http:
            return await http.get(pfad)

    return asyncio.run(go())


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> _Client:
    kurs = {"course_id": "c1", "kurs_titel": "Zwingen", "verteilung": VERTEILUNG,
            "stichworte_top": [["<script>x</script>", 2], ["klar", 1]],
            "einzeln": [{"smiley": "gut", "stichworte": ["<script>x</script>"],
                         "datum": "2026-10-30T10:00:00+00:00"}]}
    fake = _Client(kurse=[kurs])
    fake.__enter__ = lambda: fake  # type: ignore[attr-defined]
    monkeypatch.setattr(werkzeuge, "_client", lambda: _Ctx(fake))
    return fake


class _Ctx:
    def __init__(self, c: _Client) -> None:
        self.c = c

    def __enter__(self) -> _Client:
        return self.c

    def __exit__(self, *a) -> None:
        return None


def test_link_token_gueltig_abgelaufen_manipuliert(api) -> None:
    jetzt = time.time()
    gut = token_erzeugen(jetzt=jetzt)
    assert _seite(f"/bewertungen/{gut}").status_code == 200
    alt = token_erzeugen(jetzt=jetzt - 31 * 86400)
    abgelaufen = _seite(f"/bewertungen/{alt}")
    assert abgelaufen.status_code == 403
    assert "Dieser Link ist abgelaufen oder ungültig." in abgelaufen.text
    gekippt = gut[:-1] + ("A" if gut[-1] != "A" else "B")
    manip = _seite(f"/bewertungen/{gekippt}")
    assert manip.status_code == 403 and "Zwingen" not in manip.text
    assert _seite("/bewertungen/quatsch").status_code == 403


def test_seite_escaped_stichworte(api) -> None:
    seite = _seite(f"/bewertungen/{token_erzeugen()}")
    assert "&lt;script&gt;" in seite.text and "<script>x" not in seite.text
    assert "Zwingen" in seite.text and "2026-10-30" in seite.text


def test_build_app_startet_keinen_thread() -> None:
    vorher = {t.name for t in threading.enumerate()}
    build_app(object.__new__(type("S", (), {"streamable_http_app": lambda self: None})), "t" * 40)
    assert {t.name for t in threading.enumerate()} == vorher
    assert bewertungen.starte_melder.__name__ == "starte_melder"
