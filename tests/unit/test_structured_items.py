"""Hands-on items for the practice catalog, read off the lessons' structure, in the engine's forms."""

from __future__ import annotations

from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.course_factory.structured_items import structured_items
from spaces.learning.services.evaluation.deterministic import score_response
from spaces.learning.services.evaluation.schemas import DeterministicItem
from spaces.learning.tests.unit.test_lernsequenz_aus_kurs import COURSE, draft
from spaces.learning.tests.unit.test_lernsequenz_aus_struktur import BODY


def _draft_with_structure() -> CourseDraft:
    data = draft().model_dump(mode="json")
    data["lessons"][0]["body"] = BODY
    return CourseDraft.model_validate(data)


def _self_answer(spec) -> dict:
    if spec.item_type == "fill_in":
        return {"text": spec.expected_answer["answers"][0]}
    return dict(spec.expected_answer)


def test_structure_becomes_ordering_cloze_and_matching_items_in_engine_form() -> None:
    specs = structured_items(_draft_with_structure())

    assert [s.item_type for s in specs] == ["ordering", "fill_in", "fill_in", "matching"]
    assert all(s.concept_ids == ["pruefen"] and s.chapter_id == "k1" for s in specs)
    assert len({s.activity_id for s in specs}) == 4
    ordering, first_blank, _second, matching = specs
    assert sorted(ordering.expected_answer["order"]) == sorted(o["id"] for o in ordering.scoring_config["options"])
    assert ordering.expected_answer["order"] != [o["id"] for o in ordering.scoring_config["options"]]
    assert ordering.scoring_config["partial_credit"] is True
    assert first_blank.expected_answer == {"answers": ["install.ps1"]}
    assert first_blank.scoring_config["template"] == "irm https://claude.ai/{{wert}} | iex"
    assert set(matching.expected_answer["pairs"]) == {"b1", "b2", "b3"}
    assert {o["id"] for o in matching.scoring_config["right"]} == set(matching.expected_answer["pairs"].values())
    assert [o["label"] for o in matching.scoring_config["left"]] == ["Terminal", "PATH", "Installer"]


def test_every_generated_item_scores_itself_as_fully_correct() -> None:
    for spec in structured_items(_draft_with_structure()):
        item = DeterministicItem(
            item_id=spec.activity_id, item_type=spec.item_type, prompt=spec.scoring_config["prompt"],
            options=[], expected_answer=spec.expected_answer, scoring_config=spec.scoring_config,
        )
        assert score_response(item, _self_answer(spec)).score == 1.0, spec.item_type


def test_lessons_without_structure_give_no_items() -> None:
    assert structured_items(draft()) == []
    assert COURSE  # the fixture course is the one the catalog tests project
