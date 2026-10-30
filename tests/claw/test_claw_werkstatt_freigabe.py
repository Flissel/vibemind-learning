"""Werkstatt release from the claw: the model sees the comparison, never the one-time secret.

`werkstatt_freigabe_anzeigen` prepares the release and opens the approval page locally; with
`LEARNING_CLAW_FREIGABE_BASIS` set it also sends the phone link to the owner by Telegram (a
child process gets the message on stdin). Only the page calls `werkstatt_freigeben`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from spaces.learning.claw import server, werkzeuge
from spaces.learning.claw.freigabe import FreigabeStore
from spaces.learning.claw.learning_client import LearningClient

KEY = "k" * 40
API = "http://127.0.0.1:8090/api/v1"
MCP = "http://127.0.0.1:8091/mcp"
COURSE = "kurs-1"
FREIGABE = "f1f1f1f1-0000-4000-8000-000000000001"
BASIS = "https://pc.example.ts.net"


def _vorbereitet(blockiert: bool = False) -> dict[str, Any]:
    return {
        "freigabe_id": FREIGABE, "nummer": 3, "titel": "PowerShell Grundlagen",
        "learnhouse_revision": 7, "blockiert": blockiert, "nichts_zu_veroeffentlichen": False,
        "vergleich": {
            "neu": [{"task_id": "t1", "kapitel": "K1", "summary": "Dateien pruefen",
                     "fassung_alt": None, "fassung_neu": 1}],
            "geaendert": [], "textkorrektur": [], "geloescht": [], "unveraendert": 2,
        },
        "befunde": [{"code": "address_mixed", "task_id": None, "blockierend": blockiert,
                     "text": "Der Kurs mischt Du und Sie."}],
    }


class Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.blockiert = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == f"/api/v1/werkstatt/kurse/{COURSE}/freigabe":
            return httpx.Response(201, json=_vorbereitet(self.blockiert))
        if request.method == "POST" and path == f"/api/v1/werkstatt/freigaben/{FREIGABE}/freigeben":
            return httpx.Response(200, json={
                "freigabe_id": FREIGABE, "learnhouse_revision": 8, "aktualisiert": 0, "neu": 1,
                "unveroeffentlicht": 0, "katalog": {"items": 1, "konzepte": 1, "lernstand_uebernommen": 0},
            })
        return httpx.Response(404, json={"code": "not_found", "message": "x"})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    f = Fake()
    monkeypatch.setattr(
        werkzeuge, "_client",
        lambda: LearningClient(API, MCP, KEY, transport=httpx.MockTransport(f.handle)),
    )
    monkeypatch.setattr(werkzeuge, "_freigaben", FreigabeStore())
    monkeypatch.delenv("LEARNING_CLAW_FREIGABE_BASIS", raising=False)
    monkeypatch.delenv("LEARNING_CLAW_TELEGRAM_SKRIPT", raising=False)
    yield f


@pytest.fixture(autouse=True)
def browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    monkeypatch.setattr(werkzeuge, "_browser_oeffnen", lambda url: opened.append(url) or True)
    return opened


class Unterprozess:
    """Captures the Telegram child process instead of starting it."""

    def __init__(self, returncode: int = 0) -> None:
        self.aufrufe: list[dict[str, Any]] = []
        self.returncode = returncode

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.aufrufe.append({"args": list(args), **kwargs})
        import subprocess

        return subprocess.CompletedProcess(args, self.returncode)


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Unterprozess:
    skript = tmp_path / "notify_telegram.py"
    skript.write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setenv("LEARNING_CLAW_TELEGRAM_SKRIPT", str(skript))
    monkeypatch.setenv("LEARNING_CLAW_LOG_DIR", str(tmp_path / "logs"))
    fang = Unterprozess()
    monkeypatch.setattr(werkzeuge.subprocess, "run", fang)
    return fang


def _geheimnis(opened: list[str]) -> str:
    return opened[-1].rsplit("/", 1)[1]


def test_werkstatt_freigabe_anzeigen_gibt_dem_modell_kein_geheimnis(fake: Fake, browser) -> None:
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert out["ok"] is True, out
    daten = out["daten"]
    assert daten["titel"] == "PowerShell Grundlagen" and daten["nummer"] == 3
    assert daten["vergleich"]["neu"][0]["summary"] == "Dateien pruefen"
    assert daten["befunde"][0]["text"] == "Der Kurs mischt Du und Sie."
    assert daten["blockiert"] is False
    assert daten["link_geoeffnet"] is True and daten["handy_link_gesendet"] is False
    (link,) = browser
    assert link.startswith("http://127.0.0.1:8139/freigabe/")
    geheimnis = _geheimnis(browser)
    text = json.dumps(out)
    assert geheimnis not in text and "/freigabe/" not in text
    assert werkzeuge._freigaben.holen(geheimnis).approval_ref not in text
    request = fake.requests[-1]
    assert request.headers["X-VibeMind-Roles"] == "learning.author"
    assert request.headers["X-Werkstatt-Author"] == "claw"


def test_handy_link_geht_per_telegram_nicht_ans_modell(
    fake: Fake, browser, telegram: Unterprozess, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", BASIS)
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert out["ok"] is True, out
    assert out["daten"]["handy_link_gesendet"] is True
    geheimnis = _geheimnis(browser)
    (aufruf,) = telegram.aufrufe
    # The link travels on stdin, never in the command line; output goes to a file, not a pipe.
    assert all(geheimnis not in str(a) for a in aufruf["args"])
    assert aufruf["args"][-1] == str(tmp_path / "notify_telegram.py")
    nachricht = aufruf["input"].decode("utf-8")
    assert f"{BASIS}/freigabe/{geheimnis}" in nachricht
    assert "PowerShell Grundlagen" in nachricht
    assert aufruf["stdout"] is not None and hasattr(aufruf["stdout"], "write")
    assert aufruf["stderr"] is aufruf["stdout"]
    assert 0 < aufruf["timeout"] <= 30
    assert (tmp_path / "logs").is_dir()
    text = json.dumps(out)
    assert geheimnis not in text and "ts.net" not in text and "/freigabe/" not in text


def test_handy_link_fehlschlag_meldet_false(
    fake: Fake, browser, telegram: Unterprozess, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", BASIS)
    telegram.returncode = 2
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert out["ok"] is True and out["daten"]["handy_link_gesendet"] is False


def test_ohne_handy_basis_kein_telegram(fake: Fake, browser, telegram: Unterprozess) -> None:
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert out["daten"]["handy_link_gesendet"] is False
    assert telegram.aufrufe == []


@pytest.mark.parametrize("basis", ["http://pc.example.ts.net", "https://", "https://pc.example.ts.net/x"])
def test_ungueltige_handy_basis_bleibt_lokal(
    fake: Fake, browser, telegram: Unterprozess, monkeypatch: pytest.MonkeyPatch, basis: str
) -> None:
    monkeypatch.setenv("LEARNING_CLAW_FREIGABE_BASIS", basis)
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert out["daten"]["handy_link_gesendet"] is False
    assert telegram.aufrufe == []


def test_werkstatt_freigeben_gibt_einmal_mit_publisher_rolle_frei(fake: Fake, browser) -> None:
    werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    geheimnis = _geheimnis(browser)
    ref = werkzeuge._freigaben.holen(geheimnis).approval_ref
    erster = werkzeuge.werkstatt_freigeben(geheimnis)
    assert erster["ok"] is True, erster
    assert erster["daten"]["learnhouse_revision"] == 8
    assert erster["daten"]["katalog"] == {"items": 1, "konzepte": 1, "lernstand_uebernommen": 0}
    request = fake.requests[-1]
    assert request.url.path == f"/api/v1/werkstatt/freigaben/{FREIGABE}/freigeben"
    assert request.headers["X-VibeMind-Roles"] == "learning.publisher"
    assert json.loads(request.content) == {"approval_ref": ref}
    zweiter = werkzeuge.werkstatt_freigeben(geheimnis)
    assert zweiter["ok"] is False
    assert sum(r.url.path.endswith("/freigeben") for r in fake.requests) == 1


def test_werkstatt_freigeben_wartet_laenger_als_andere_aufrufe(fake: Fake, browser) -> None:
    # Writing LearnHouse and the practice catalog can take longer than a normal request.
    werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    vorbereiten = fake.requests[-1]
    werkzeuge.werkstatt_freigeben(_geheimnis(browser))
    freigeben = fake.requests[-1]
    assert freigeben.url.path.endswith("/freigeben")
    assert freigeben.extensions["timeout"]["read"] == 120
    assert vorbereiten.extensions["timeout"]["read"] == 30


def test_kurs_und_werkstatt_geheimnisse_sind_getrennt(fake: Fake, browser) -> None:
    werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    geheimnis = _geheimnis(browser)
    # The factory's publish path must not use up (or act on) a Werkstatt approval ...
    assert werkzeuge.freigeben(geheimnis)["ok"] is False
    assert werkzeuge._freigaben.holen(geheimnis)
    # ... and the Werkstatt path not a factory one.
    kurs = werkzeuge._freigaben.merken("job-1", 1, 1, {"job_id": "job-1"})
    assert werkzeuge.werkstatt_freigeben(kurs.geheimnis)["ok"] is False
    assert werkzeuge._freigaben.holen(kurs.geheimnis)
    assert not any(r.url.path.endswith("/freigeben") for r in fake.requests)


def test_kein_werkzeug_gibt_frei() -> None:
    namen = {f.__name__ for f in server.WERKZEUGE}
    assert "werkstatt_freigabe_anzeigen" in namen
    assert namen.isdisjoint({"werkstatt_freigeben", "freigeben", "pruefung_entscheiden"})
    assert not any(n.endswith("freigeben") for n in namen)



def test_neu_ausliefern_geht_an_die_route(fake: Fake, browser) -> None:
    werkzeuge.werkstatt_freigabe_anzeigen(COURSE)
    assert json.loads(fake.requests[-1].content or b"{}") == {}
    out = werkzeuge.werkstatt_freigabe_anzeigen(COURSE, neu_ausliefern=True)
    assert out["ok"] is True, out
    assert json.loads(fake.requests[-1].content) == {"neu_ausliefern": True}
    assert "neu_ausliefern" in (werkzeuge.werkstatt_freigabe_anzeigen.__doc__ or "")
