"""Model-proposed checkpoints are validated and grounded before they enter a sequence."""

from __future__ import annotations

import asyncio

from spaces.learning.contracts.lernsequenz import LernsequenzV1
from spaces.learning.services.course_factory.model_gateway import GatewayResult
from spaces.learning.services.lernsequenz.anreichern import enrich

CORRELATION = "11111111-1111-4111-8111-111111111111"


def sequence() -> LernsequenzV1:
    return LernsequenzV1.model_validate({
        "sequence_id": "praes-akku", "course_id": "kurs", "title": "Akku", "kind": "presentation",
        "sections": [
            {"kind": "slide", "section_id": "s1", "title": "Laden",
             "narration": "Ein Akku mit 4 Ah lädt am Ladegerät mit 2 A. Der Akku wird erst geladen, dann gelagert.",
             "refs": ["c1"]},
            {"kind": "slide", "section_id": "s2", "title": "Lagern", "narration": "Kühl und trocken lagern.",
             "refs": ["c2"]},
        ],
    })


class _Gateway:
    def __init__(self, proposals):
        self.proposals = proposals
        self.invocation = None

    async def generate(self, invocation, output_model):
        self.invocation = invocation
        return GatewayResult(
            output=output_model.model_validate({"schema_version": "sequence-design-v1", "checkpoints": self.proposals}),
            evidence_ref="openfang://completion/x",
        )


GOOD_CALC = {"kind": "calculation", "after_section": "s1", "title": "Ladezeit",
             "task": "Ein Akku mit 4 Ah lädt mit 2 A. Wie lange dauert es?",
             "steps": [{"step_id": "h", "title": "Stunden", "unit": "h", "hint": "Ah durch A", "expected": "4/2"}]}
INVENTED_CALC = {**GOOD_CALC, "task": "Ein Akku mit 5 Ah lädt mit 2 A.",
                 "steps": [{"step_id": "h", "title": "Stunden", "unit": "h", "hint": "x", "expected": "5/2"}]}
DRAWING = {"kind": "drawn_explanation", "after_section": "s1", "title": "Ablauf",
           "steps": [{"narration": "Erst laden.", "strokes": [{"path": "M40 120 h150 v90 h-150 z"}],
                      "labels": [{"x": 115, "y": 170, "text": "Laden"}]},
                     {"narration": "Dann lagern.", "strokes": [{"path": "M200 165 h100", "color": "accent"}]}]}
OFF_BOARD = {**DRAWING, "steps": [{"narration": "x", "strokes": [{"path": "M40 120 h1500"}]}]}


def test_grounded_proposals_are_added_with_slide_refs() -> None:
    gateway = _Gateway([GOOD_CALC, DRAWING])
    enriched, report = asyncio.run(enrich(gateway, sequence(), correlation_id=CORRELATION))

    assert [c.kind for c in enriched.checkpoints] == ["calculation", "drawn_explanation"]
    assert all(c.refs == ("c1",) for c in enriched.checkpoints)
    assert gateway.invocation.role == "sequence_designer"
    assert "4 Ah" in gateway.invocation.input_payload["slides"][0]["text"]
    assert report == ["#1: added calculation after s1", "#2: added drawn_explanation after s1"]


def test_svg_style_strokes_and_missing_titles_are_accepted() -> None:
    # Live (25.10.): the model wrote strokes as {"d": ...} and left out titles.
    proposal = {k: v for k, v in DRAWING.items() if k != "title"}
    proposal["steps"] = [{"narration": "Erst laden.", "strokes": [{"d": "M40 120 h150 v90 h-150 z"}]}]
    enriched, report = asyncio.run(enrich(_Gateway([proposal]), sequence(), correlation_id=CORRELATION))
    assert report == ["#1: added drawn_explanation after s1"]
    assert enriched.checkpoints[0].title == "An der Tafel erklärt"
    assert enriched.checkpoints[0].steps[0].strokes[0].path == "M40 120 h150 v90 h-150 z"


def test_invented_numbers_off_board_drawings_and_bad_shapes_are_rejected() -> None:
    proposals = [INVENTED_CALC, OFF_BOARD, {"kind": "estimate", "after_section": "s9"}, {"kind": "choice", "after_section": "s1"}]
    enriched, report = asyncio.run(enrich(_Gateway(proposals), sequence(), correlation_id=CORRELATION))

    assert enriched.checkpoints == ()
    assert report[0].startswith("#1: rejected, numbers not on the slide: [5.0]")
    assert report[1] == "#2: rejected, drawing leaves the board"
    assert report[2] == "#3: unknown slide"
    assert report[3].startswith("#4: invalid")
