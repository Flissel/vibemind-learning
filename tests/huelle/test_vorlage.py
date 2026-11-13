"""Vorlagen (Matrix, Onepager) als Text und Tinte im Arbeitsfeld."""

from __future__ import annotations

from spaces.learning.huelle.vorlage import VORLAGEN, vorlagenfeld, vorlagenplan

FELD = {"x": 600, "y": 180, "w": 1480, "h": 1240}


def _innen(r: dict[str, int], feld: dict[str, int]) -> bool:
    return (r["x"] >= feld["x"] and r["y"] >= feld["y"]
            and r["x"] + r["w"] <= feld["x"] + feld["w"] and r["y"] + r["h"] <= feld["y"] + feld["h"])


def test_vorlagen_konstante() -> None:
    assert VORLAGEN == ("matrix", "onepager")


def test_matrix_hat_vier_feldnamen_und_zwei_achsen() -> None:
    plan = vorlagenplan("matrix", FELD)
    assert plan is not None
    assert [t for t, _ in plan.texte] == [
        "Nutzen ↑", "Aufwand →", "Quick Wins", "Große Vorhaben", "Nebenbei", "Lieber nicht"]
    assert all(_innen(r, FELD) and r["h"] == 40 for _, r in plan.texte)
    assert len(plan.striche) == 4  # zwei Achsen, zwei Teilungslinien
    for strich in plan.striche:
        assert len(strich) == 2
        assert all(FELD["x"] <= p["x"] <= FELD["x"] + FELD["w"] and FELD["y"] <= p["y"] <= FELD["y"] + FELD["h"]
                   for p in strich)
    namen = dict(plan.texte)
    # Quick Wins: viel Nutzen (oben), wenig Aufwand (links); Lieber nicht: unten rechts.
    assert namen["Quick Wins"]["x"] < namen["Große Vorhaben"]["x"]
    assert namen["Quick Wins"]["y"] == namen["Große Vorhaben"]["y"]
    assert namen["Nebenbei"]["y"] > namen["Quick Wins"]["y"] and namen["Lieber nicht"]["x"] > namen["Nebenbei"]["x"]


def test_onepager_hat_sechs_abschnitte() -> None:
    plan = vorlagenplan("onepager", FELD)
    assert plan is not None
    assert [t for t, _ in plan.texte] == [
        "1. Ausgangslage", "2. Empfohlene Use Cases", "3. Nutzen", "4. Aufwand", "5. Risiken", "6. Nächster Schritt"]
    assert all(_innen(r, FELD) for _, r in plan.texte)
    abstaende = {b["y"] - a["y"] for (_, a), (_, b) in zip(plan.texte, plan.texte[1:])}
    assert len(abstaende) == 1  # Zeilen gleicher Höhe
    assert len(plan.striche) == 5
    assert all(len(s) == 2 and s[0]["y"] == s[1]["y"] for s in plan.striche)


def test_unbekannte_vorlage_ergibt_none() -> None:
    assert vorlagenplan("sonstwas", FELD) is None


def test_vorlagenfeld_mit_bildern_ist_untere_haelfte() -> None:
    a = {"x": 600, "y": 180, "w": 1480, "h": 1240}
    assert vorlagenfeld(a, mit_bildern=False) == a
    unten = vorlagenfeld(a, mit_bildern=True)
    assert unten["y"] == 180 + 620 + 20 and unten["x"] == 600 and unten["w"] == 1480
    assert unten["y"] + unten["h"] == a["y"] + a["h"]
