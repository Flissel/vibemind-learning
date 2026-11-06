from __future__ import annotations

import pytest

from spaces.learning.services.course_factory.model_gateway import (
    GatewayResult,
    GatewaySchemaError,
)
from spaces.learning.services.evaluation.handschrift import GelesenV1, HandschriftLeser

PNG = b"\x89PNG\r\n\x1a\nhandschrift"
TASK = {
    "id": "t1",
    "type": "single_choice",
    "prompt": "Welche Aussage stimmt?",
    "options": [{"id": "a", "label": "Eins"}, {"id": "b", "label": "Zwei"}],
    "presentation": {},
}


class _Gateway:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.invocations = []

    async def generate(self, invocation, output_model):
        self.invocations.append(invocation)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return GatewayResult(output=self.outcome, evidence_ref="openfang://completion/hs-1")


def _lesen(outcome, task=TASK):
    gateway = _Gateway(outcome)
    result = HandschriftLeser(lambda: gateway).lesen(task, PNG)
    return result, gateway


def test_lesbar_mit_etikett_ergibt_antwort() -> None:
    (gelesen, antwort), _ = _lesen(GelesenV1(lesbar=True, roh={"auswahl": ["B"]}, text="B"))
    assert gelesen.lesbar is True
    assert antwort == {"choice_id": "b"}


def test_leere_lesung_ist_nicht_lesbar() -> None:
    (gelesen, antwort), _ = _lesen(GelesenV1(lesbar=True, roh={}, text=""))
    assert gelesen.lesbar is False
    assert antwort is None


def test_unbekanntes_etikett_ist_nicht_lesbar() -> None:
    (gelesen, antwort), _ = _lesen(GelesenV1(lesbar=True, roh={"auswahl": ["E"]}, text="E"))
    assert gelesen.lesbar is False
    assert antwort is None


def test_modell_meldet_unlesbar_wird_nicht_bewertet() -> None:
    (gelesen, antwort), _ = _lesen(GelesenV1(lesbar=False, roh={"auswahl": ["B"]}, text="?"))
    assert gelesen.lesbar is False
    assert antwort is None


def test_invocation_hat_ein_png_und_keine_erwartete_antwort() -> None:
    task = {
        **TASK,
        "expected_answer": {"choice_id": "a"},
        "correct_option_ids": ["a"],
        "scoring_config": {"x": 1},
    }
    _, gateway = _lesen(GelesenV1(lesbar=True, roh={"auswahl": ["A"]}, text="A"), task)
    (invocation,) = gateway.invocations
    assert len(invocation.image_inputs) == 1
    assert invocation.image_inputs[0].media_type == "image/png"
    assert invocation.prompt_version == "handschrift-lesen-v1"
    text = repr(invocation.input_payload).lower()
    assert "expected" not in text
    assert "correct_option" not in text
    assert "scoring" not in text
    assert "Welche Aussage stimmt?" in repr(invocation.input_payload)
    assert invocation.input_payload["etiketten"]["optionen"] == {"A": "Eins", "B": "Zwei"}


def test_modellfehler_wird_nicht_zur_lesung() -> None:
    with pytest.raises(GatewaySchemaError):
        _lesen(GatewaySchemaError("invalid json"))


def test_kein_png_wird_vor_dem_modell_abgelehnt() -> None:
    gateway = _Gateway(GelesenV1(lesbar=True, roh={}, text=""))
    with pytest.raises(ValueError):
        HandschriftLeser(lambda: gateway).lesen(TASK, b"not a png")
    assert gateway.invocations == []


def test_text_ist_auf_200_zeichen_begrenzt() -> None:
    with pytest.raises(ValueError):
        GelesenV1(lesbar=True, roh={}, text="x" * 201)
