"""What of an item's presentation reaches the learner, per item type (never the solution)."""

from __future__ import annotations

from types import SimpleNamespace

from spaces.learning.services.adaptive_engine.session_service import AdaptiveSessionService


def _item(item_type: str, presentation: dict) -> SimpleNamespace:
    return SimpleNamespace(item_type=item_type, scoring_config={"presentation": presentation})


def test_matching_shows_both_sides_but_never_the_pairs_solution() -> None:
    shown = AdaptiveSessionService._presentation(_item("matching", {
        "left": [{"id": "b1", "label": "Terminal"}], "right": [{"id": "e1", "label": "Fenster für Befehle"}],
        "pairs": {"b1": "e1"}, "solution": "x",
    }))
    assert shown["left"][0]["label"] == "Terminal" and shown["right"][0]["id"] == "e1"
    assert "solution" not in shown and "pairs" not in shown


def test_fill_in_shows_the_template_and_shell_only() -> None:
    shown = AdaptiveSessionService._presentation(_item("fill_in", {
        "template": "irm https://claude.ai/{{wert}} | iex", "shell": "powershell", "answers": ["install.ps1"],
    }))
    assert shown == {"template": "irm https://claude.ai/{{wert}} | iex", "shell": "powershell"}


def test_ordering_shows_nothing_extra() -> None:
    assert AdaptiveSessionService._presentation(_item("ordering", {"order": ["s1", "s2"]})) == {}


def test_an_image_fill_in_shows_picture_and_boxes_but_no_answer() -> None:
    shown = AdaptiveSessionService._presentation(_item("fill_in", {
        "image": "/lernsequenz-medien/term-0123456789abcdef.png", "width": 560, "height": 96, "shell": "powershell",
        "rect": {"x": 300, "y": 54, "w": 120, "h": 22}, "more": [{"x": 10, "y": 80, "w": 120, "h": 22}],
        "answers": ["install.ps1"],
    }))
    assert set(shown) == {"image", "width", "height", "shell", "rect", "more"}
