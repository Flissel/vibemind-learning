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
    assert first_blank.scoring_config["presentation"]["template"] == "irm https://claude.ai/{{wert}} | iex"
    assert set(matching.expected_answer["pairs"]) == {"b1", "b2", "b3"}
    shown = matching.scoring_config["presentation"]
    assert {o["id"] for o in shown["right"]} == set(matching.expected_answer["pairs"].values())
    assert [o["label"] for o in shown["left"]] == ["Terminal", "PATH", "Installer"]


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


def test_fill_in_items_demand_the_command_form_not_just_its_letters() -> None:
    # Review finding: normalize=True let "version" pass for "--version".
    spec = next(s for s in structured_items(_draft_with_structure()) if s.item_type == "fill_in")
    item = DeterministicItem(item_id="x", item_type="fill_in", prompt="p", options=[],
                             expected_answer=spec.expected_answer, scoring_config=spec.scoring_config)
    assert score_response(item, {"text": "install.ps1"}).score == 1.0
    assert score_response(item, {"text": " Install.PS1 "}).score == 1.0      # case and padding forgiven
    assert score_response(item, {"text": "installps1"}).score == 0.0
    assert score_response(item, {"text": "install-ps1"}).score == 0.0
    dash = DeterministicItem(item_id="y", item_type="fill_in", prompt="p", options=[],
                             expected_answer={"answers": ["--version"]}, scoring_config={"prompt": "p", "normalize": "command"})
    assert score_response(dash, {"text": "—version"}).score == 1.0           # typographic dash forgiven
    assert score_response(dash, {"text": "version"}).score == 0.0
    assert score_response(dash, {"text": "-version"}).score == 0.0


# --- Bildaufgaben (Plan 2026-10-27-learning-bildaufgaben, Task 5) ----------------------

def test_image_cloze_becomes_a_fill_in_with_an_image(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))

    specs = [s for s in structured_items(_draft_with_structure()) if s.item_type == "fill_in"]

    pictured = [s for s in specs if "image" in s.scoring_config["presentation"]]
    assert {s.scoring_config["presentation"]["shell"] for s in pictured} == {"powershell", "unix"}
    shown = next(s for s in pictured if s.scoring_config["presentation"]["shell"] == "powershell")
    presentation = shown.scoring_config["presentation"]
    assert presentation["image"].startswith("/lernsequenz-medien/term-") and presentation["width"] == 560
    assert set(presentation["rect"]) == {"x", "y", "w", "h"} and "template" not in presentation
    assert shown.expected_answer == {"answers": ["install.ps1"]} and shown.scoring_config["normalize"] == "command"
    assert "install.ps1" not in str(presentation)


def test_window_matching_items_carry_their_images(tmp_path, monkeypatch) -> None:
    from spaces.learning.tests.unit.test_lernsequenz_aus_struktur import THREE_SHELLS

    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    data = _draft_with_structure().model_dump(mode="json")
    data["lessons"][0]["body"] = THREE_SHELLS
    specs = structured_items(CourseDraft.model_validate(data))

    [windows] = [s for s in specs if s.item_type == "matching"]
    assert all(entry["image"].startswith("/lernsequenz-medien/") and entry["height"] >= 50
               for entry in windows.scoring_config["presentation"]["left"])
