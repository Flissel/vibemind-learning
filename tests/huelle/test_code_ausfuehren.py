"""Route `/sitzungen/{sid}/ausfuehren`: Code der lernenden Person ueber den Code-Laeufer (huelle/laeufer)."""

from __future__ import annotations

from typing import Any

from spaces.learning.huelle.laeufer import Lauf, LaeuferNichtBereit, UngueltigerAuftrag

from .test_bruecke import ACTOR, SID, _offene_sitzung, als, turn

PFAD = f"/lernen/api/sitzungen/{SID}/ausfuehren"


class FakeLaeufer:
    def __init__(self, ergebnis: Lauf | Exception) -> None:
        self.ergebnis = ergebnis
        self.aufrufe: list[tuple[Any, ...]] = []

    def __call__(self, sprache: Any, quelltext: Any, eingabe: Any = "") -> Lauf:
        self.aufrufe.append((sprache, quelltext, eingabe))
        if isinstance(self.ergebnis, Exception):
            raise self.ergebnis
        return self.ergebnis


def _mit_laeufer(ergebnis: Lauf | Exception):
    _, client, _, _ = _offene_sitzung({"session": turn()})
    laeufer = FakeLaeufer(ergebnis)
    client.app._laeufer = laeufer
    return client, laeufer


def test_ausfuehren_gibt_das_ergebnis_des_laeufers_zurueck() -> None:
    client, laeufer = _mit_laeufer(Lauf(0, "42\n", "", False, False, 17))
    r = client.post(PFAD, json={"sprache": "python", "quelltext": "print(6*7)", "eingabe": "a"})
    assert r.status_code == 200, r.text
    assert r.json() == {"exit": 0, "stdout": "42\n", "stderr": "", "zeit_ueberschritten": False,
                        "abgeschnitten": False, "dauer_ms": 17}
    assert laeufer.aufrufe == [("python", "print(6*7)", "a")]
    assert r.headers["cache-control"] == "no-store"


def test_ausfuehren_ohne_eingabe_und_mit_zeitueberschreitung() -> None:
    client, laeufer = _mit_laeufer(Lauf(None, "", "", True, False, 5000))
    r = client.post(PFAD, json={"sprache": "javascript", "quelltext": "for(;;){}"})
    assert r.status_code == 200 and r.json()["zeit_ueberschritten"] is True and r.json()["exit"] is None
    assert laeufer.aufrufe == [("javascript", "for(;;){}", "")]


def test_ausfuehren_ungueltiger_auftrag_ist_400() -> None:
    client, _ = _mit_laeufer(UngueltigerAuftrag("sprache"))
    r = client.post(PFAD, json={"sprache": "bash", "quelltext": "x"})
    assert r.status_code == 400 and r.json()["code"] == "code_ungueltig"
    assert "bash" not in r.text  # keine Eingaben zurueckspiegeln


def test_ausfuehren_laeufer_nicht_bereit_ist_503() -> None:
    client, _ = _mit_laeufer(LaeuferNichtBereit("exit 1"))
    r = client.post(PFAD, json={"sprache": "python", "quelltext": "print(1)"})
    assert r.status_code == 503 and r.json()["code"] == "laeufer_weg"
    assert "exit 1" not in r.text


def test_ausfuehren_nur_in_der_eigenen_sitzung() -> None:
    client, laeufer = _mit_laeufer(Lauf(0, "", "", False, False, 1))
    fremd = als(client, "lh-1-andere")
    assert fremd.post(PFAD, json={"sprache": "python", "quelltext": "print(1)"}).status_code == 404
    als(client, ACTOR)
    assert client.post("/lernen/api/sitzungen/keine-uuid/ausfuehren",
                       json={"sprache": "python", "quelltext": "print(1)"}).status_code == 404
    assert laeufer.aufrufe == []
