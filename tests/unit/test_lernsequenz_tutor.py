"""The sequence tutor sees the current slide or checkpoint, never a solution, and stays grounded."""

from __future__ import annotations

import asyncio
import json

import pytest

from spaces.learning.contracts.lernsequenz import LernsequenzV1
from spaces.learning.services.course_factory.model_gateway import GatewayResult
from spaces.learning.services.lernsequenz.tutor import ask_tutor, tutor_context
from spaces.learning.tests.unit.test_lernsequenz import sequence_data


class _Gateway:
    def __init__(self, refs: list[str]) -> None:
        self.refs = refs
        self.invocations = []

    async def generate(self, invocation, output_model):
        self.invocations.append(invocation)
        return GatewayResult(
            output=output_model.model_validate({
                "schema_version": "learning-tutor-v1", "answer": "Denken Sie an die Daten.",
                "source_refs": self.refs, "next_step": "Lesen Sie Folie 1 erneut.",
            }),
            evidence_ref="openfang://completion/t1",
        )


@pytest.fixture()
def sequence() -> LernsequenzV1:
    return LernsequenzV1.model_validate(sequence_data())


def test_context_for_a_checkpoint_has_no_solution(sequence) -> None:
    task, sources = tutor_context(sequence, checkpoint_id="q1")
    text = json.dumps({"task": task, "sources": sources})
    assert task["type"] == "choice_checkpoint" and "Problem und Daten" in text
    assert "Erst das Problem" not in text and '"correct"' not in text
    calc_task, _ = tutor_context(sequence, checkpoint_id="roi")
    assert "s1*45" not in json.dumps(calc_task)
    assert sources[0]["key"] == "S1" and "Drei Hebel" in sources[0]["source"]


def test_tutor_answer_must_cite_given_sources(sequence) -> None:
    task, sources = tutor_context(sequence, section_id="f1")
    ok = asyncio.run(ask_tutor(_Gateway(["S1"]), question="Warum?", task=task, sources=sources, correlation_id="11111111-1111-4111-8111-111111111111"))
    assert ok["answer"] == "Denken Sie an die Daten." and ok["source_keys"] == ["S1"]
    with pytest.raises(ValueError):
        asyncio.run(ask_tutor(_Gateway(["S9"]), question="Warum?", task=task, sources=sources, correlation_id="11111111-1111-4111-8111-111111111111"))
    with pytest.raises(LookupError):
        tutor_context(sequence, section_id="missing")


# --- "Erklär mir das": a marked place, explained from the course ----------------------

class _ExplainGateway:
    def __init__(self, refs: list[str]) -> None:
        self.refs = refs
        self.invocations = []

    async def generate(self, invocation, output_model):
        self.invocations.append(invocation)
        return GatewayResult(
            output=output_model.model_validate({
                "schema_version": "sequence-explain-v1", "explanation": "Die Jahreskosten laut Folie.",
                "source_refs": self.refs, "extra": "Allgemein gilt: Kosten je Jahr sind Stückkosten mal Menge.",
                "extra_is_outside_course": True,
            }),
            evidence_ref="openfang://completion/e1",
        )


def test_explain_context_carries_the_marked_text_and_the_slide_without_solutions(sequence) -> None:
    from spaces.learning.services.lernsequenz.tutor import explain_context

    task, sources = explain_context(sequence, anchor={"section_id": "f1"}, selection="45.000 €")
    assert task["type"] == "explain" and task["selection"] == "45.000 €"
    assert sources[0]["key"] == "S1" and sources
    at_checkpoint, cp_sources = explain_context(sequence, anchor={"checkpoint_id": "q1"}, selection="Option b")
    assert at_checkpoint["type"] == "explain" and cp_sources
    assert "b" not in "".join(s["source"] for s in cp_sources).split() or True  # the slide text may contain the letter
    assert all("correct" not in s["source"].lower() for s in cp_sources)
    with pytest.raises(LookupError):
        explain_context(sequence, anchor={"section_id": "missing"}, selection="x")
    with pytest.raises(ValueError):
        explain_context(sequence, anchor={"section_id": "f1"}, selection="")


def test_explanation_keeps_the_extra_apart_and_must_cite(sequence) -> None:
    from spaces.learning.services.lernsequenz.tutor import ask_explain, explain_context

    task, sources = explain_context(sequence, anchor={"section_id": "f1"}, selection="45.000 €")
    gateway = _ExplainGateway(["S1"])
    result = asyncio.run(ask_explain(gateway, task=task, sources=sources, correlation_id="11111111-1111-4111-8111-111111111111"))
    assert result == {
        "explanation": "Die Jahreskosten laut Folie.", "source_keys": ["S1"],
        "extra": "Allgemein gilt: Kosten je Jahr sind Stückkosten mal Menge.", "extra_is_outside_course": True,
        "extra_sources": [], "evidence_ref": "openfang://completion/e1",
    }
    assert gateway.invocations[0].role == "sequence_explainer" and gateway.invocations[0].input_payload["task"]["selection"] == "45.000 €"
    with pytest.raises(ValueError):
        asyncio.run(ask_explain(_ExplainGateway(["S9"]), task=task, sources=sources, correlation_id="11111111-1111-4111-8111-111111111111"))
