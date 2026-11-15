"""Text der Rueckmeldungskarte: zuerst, was fehlt; dann, was gelungen ist."""

from __future__ import annotations

from typing import Any

from spaces.learning.huelle.bruecke import KARTE_NOCH_NICHT, KARTE_RICHTIG, kartentext


def _vb(zustand: str, *kriterien: tuple[str, float, str]) -> dict[str, Any]:
    return {"lesbar": True, "feedback": {"state": zustand, "criteria": [
        {"criterion_id": f"k{i}", "description": b, "score": s, "feedback": t}
        for i, (b, s, t) in enumerate(kriterien)
    ]}}


def test_teilweise_nennt_zuerst_das_fehlende_dann_das_gelungene() -> None:
    text = kartentext(_vb(
        "partial",
        ("Nennt den Nutzen.", 1, "Der Nutzen ist klar."),
        ("Nennt eine Zahl", 0.5, "Die Zahl fehlt (S1)."),
        ("Nennt ein Risiko:", 0, "Kein Risiko genannt."),
    ))
    assert text == (
        "Es fehlt noch:\n– Nennt eine Zahl: Die Zahl fehlt.\n– Nennt ein Risiko: Kein Risiko genannt."
        "\n\nGut gelungen:\n– Nennt den Nutzen"
        f"\n\n{KARTE_NOCH_NICHT}"
    )


def test_richtig_mit_kleinem_wunsch_sagt_was_zur_vollen_punktzahl_fehlt() -> None:
    # Ab 0,7 ist eine Rubrik-Antwort richtig; der offene Punkt steht trotzdem vorn.
    text = kartentext(_vb("correct", ("Nennt den Nutzen", 1, "Klar."), ("Nennt eine Zahl", 0.5, "Die Zahl fehlt.")))
    assert text == (
        "Für die volle Punktzahl fehlt noch:\n– Nennt eine Zahl: Die Zahl fehlt."
        "\n\nGut gelungen:\n– Nennt den Nutzen"
        f"\n\n{KARTE_RICHTIG}"
    )


def test_alles_erfuellt_zaehlt_das_gelungene_auf() -> None:
    text = kartentext(_vb("correct", ("Nennt den Nutzen", 1, "Klar."), ("Nennt eine Zahl", 1.0, "Stimmt.")))
    assert text == f"Gut gelungen:\n– Nennt den Nutzen\n– Nennt eine Zahl\n\n{KARTE_RICHTIG}"


def test_ohne_kriterien_bleibt_der_kurze_satz() -> None:
    assert kartentext(_vb("correct")) == KARTE_RICHTIG
    assert kartentext(_vb("partial")) == KARTE_NOCH_NICHT


def test_kriterium_ohne_text_oder_beschreibung() -> None:
    # Fehlendes ohne Rueckmeldetext: die Beschreibung allein; ohne Beschreibung: der Text allein.
    text = kartentext(_vb("incorrect", ("Nennt eine Zahl", 0, ""), ("", 0, "Bitte ergänzen.")))
    assert text == f"Es fehlt noch:\n– Nennt eine Zahl\n– Bitte ergänzen.\n\n{KARTE_NOCH_NICHT}"


def _fest(score: float, code: str) -> dict[str, Any]:
    return {"lesbar": True, "score": score,
            "feedback": {"state": "partial" if score >= 0.65 else "incorrect", "score": score,
                         "rationale_codes": [code], "criteria": []}}


ZUORDNEN = {"type": "matching", "presentation": {"left": [{"id": f"l{i}"} for i in range(6)], "right": []}}
ORDNEN = {"type": "ordering", "options": [{"id": f"o{i}"} for i in range(4)]}


def test_zuordnen_nennt_wie_viele_paare_stimmen() -> None:
    text = kartentext(_fest(4 / 6, "partial_match"), ZUORDNEN)
    assert text == f"4 von 6 Zuordnungen stimmen.\n\n{KARTE_NOCH_NICHT}"


def test_ordnen_nennt_wie_viele_an_der_richtigen_stelle_stehen() -> None:
    text = kartentext(_fest(0.5, "partial_match"), ORDNEN)
    assert text == f"2 von 4 Kärtchen stehen an der richtigen Stelle.\n\n{KARTE_NOCH_NICHT}"


def test_ohne_teilpunkte_oder_ohne_aufgabe_keine_zahl() -> None:
    # Ohne Teilpunkte ist die Punktzahl 0, die Zahl der Treffer steckt dann nicht darin.
    assert kartentext(_fest(0.0, "incorrect_matching"), ZUORDNEN) == KARTE_NOCH_NICHT
    assert kartentext(_fest(4 / 6, "partial_match")) == KARTE_NOCH_NICHT
    assert kartentext(_fest(4 / 6, "partial_match"), {"type": "matching", "presentation": {}}) == KARTE_NOCH_NICHT
