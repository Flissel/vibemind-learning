"""Hands-on checkpoints: ordering, cloze, matching, hotspot, terminal sim, step, profile."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spaces.learning.contracts.lernsequenz import LernsequenzV1, public_view
from spaces.learning.services.lernsequenz.befehlspruefungen import claude_installiert
from spaces.learning.services.lernsequenz.bewertung import grade
from spaces.learning.tests.unit.test_lernsequenz import sequence_data

MITMACH_KINDS = {"ordering", "cloze", "matching", "hotspot", "terminal_sim", "step", "profile"}


def mitmach_data() -> dict:
    data = sequence_data()
    data["sections"][1]["only_for"] = [{"key": "shell", "option": "powershell"}]
    data["checkpoints"] = [
        {"kind": "profile", "checkpoint_id": "pr", "after_section": "f1", "title": "Dein Weg",
         "questions": [{"key": "shell", "question": "Welche Shell nutzt du?", "options": [
             {"option_id": "powershell", "text": "Windows PowerShell"}, {"option_id": "cmd", "text": "Windows CMD"},
             {"option_id": "unix", "text": "macOS oder Linux"}]}]},
        {"kind": "ordering", "checkpoint_id": "ord", "after_section": "f1", "title": "Reihenfolge",
         "task": "Bringe die Schritte in die richtige Reihenfolge.",
         "items": [{"item_id": "konto", "text": "Konto anlegen"}, {"item_id": "laden", "text": "Installer herunterladen"},
                   {"item_id": "anmelden", "text": "Anmelden"}],
         "correct": ["konto", "laden", "anmelden"], "refs": ["c1"]},
        {"kind": "cloze", "checkpoint_id": "cl", "after_section": "f1", "title": "Befehl vervollständigen",
         "task": "Ergänze den Installationsbefehl für PowerShell.",
         "template": "irm https://claude.ai/{{datei}} | iex",
         "blanks": [{"blank_id": "datei", "accepted": ["install.ps1"]}], "shell": "powershell"},
        {"kind": "matching", "checkpoint_id": "mt", "after_section": "f1", "title": "Zuordnen",
         "task": "Welcher Befehl gehört zu welcher Shell?",
         "left": [{"item_id": "l1", "text": "irm … | iex"}, {"item_id": "l2", "text": "curl … install.cmd"},
                  {"item_id": "l3", "text": "curl … | bash"}],
         "right": [{"item_id": "r1", "text": "PowerShell"}, {"item_id": "r2", "text": "CMD"}, {"item_id": "r3", "text": "macOS / Linux"}],
         "pairs": {"l1": "r1", "l2": "r2", "l3": "r3"}},
        {"kind": "hotspot", "checkpoint_id": "hs", "after_section": "f2", "title": "Stelle markieren",
         "task": "Markiere, wo du Claude Code öffnest.", "image": "/lernsequenz-medien/app.png",
         "width": 1200, "height": 800, "target": {"x": 300, "y": 40, "w": 80, "h": 30}},
        {"kind": "terminal_sim", "checkpoint_id": "ts", "after_section": "f2", "title": "Übungs-Terminal",
         "task": "Prüfe die Installation.", "shell": "powershell", "prompt": "PS C:\\Users\\anna>",
         "commands": [{"pattern": r"^claude\s+--version$", "output": "2.1.211 (Claude Code)", "reaches_goal": True},
                      {"pattern": r"^claude\s+doctor$", "output": "Install health: ok"}],
         "unknown_output": "Der Befehl ist nicht bekannt. Versuche es mit claude --version."},
        {"kind": "step", "checkpoint_id": "st", "after_section": "f2", "title": "Schritt 4",
         "instruction": "Öffne ein neues Terminal und prüfe die Installation.",
         "commands": [{"shell": "powershell", "command": "claude --version"}],
         "expected_text": "2.1.211 (Claude Code)",
         "check": claude_installiert("st-check", "f2").model_dump(mode="json")},
    ]
    return data


def mitmach_sequence() -> LernsequenzV1:
    return LernsequenzV1.model_validate(mitmach_data())


def test_every_hands_on_kind_validates() -> None:
    assert {c.kind for c in mitmach_sequence().checkpoints} == MITMACH_KINDS


def test_ordering_correct_must_be_a_permutation() -> None:
    data = mitmach_data()
    data["checkpoints"][1]["correct"] = ["konto", "laden"]
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)
    data["checkpoints"][1]["correct"] = ["konto", "konto", "laden"]
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)


def test_cloze_placeholders_need_blanks() -> None:
    data = mitmach_data()
    data["checkpoints"][2]["template"] = "irm https://claude.ai/{{datei}} | {{tool}}"
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)


def test_matching_pairs_must_reference_items() -> None:
    data = mitmach_data()
    data["checkpoints"][3]["pairs"] = {"l1": "r9", "l2": "r2", "l3": "r3"}
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)
    data["checkpoints"][3]["pairs"] = {"l1": "r1"}
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)


def test_public_view_hides_every_secret() -> None:
    view = {c["checkpoint_id"]: c for c in public_view(mitmach_sequence())["checkpoints"]}
    assert "correct" not in view["ord"]
    assert "blanks" not in view["cl"] and view["cl"]["blank_ids"] == ["datei"]
    assert "pairs" not in view["mt"] and len(view["mt"]["right"]) == 3
    assert "target" not in view["hs"]
    assert "commands" not in view["ts"] and "unknown_output" not in view["ts"]
    assert "check" not in view["st"] and view["st"]["has_check"] is True


def test_public_view_shuffles_ordering_items_and_matching_partners() -> None:
    # The authored item order is the solution; the browser must get a fixed shuffle instead.
    data = mitmach_data()
    data["checkpoints"][1]["items"] = [{"item_id": f"s{i}", "text": f"Schritt {i}"} for i in range(1, 9)]
    data["checkpoints"][1]["correct"] = [f"s{i}" for i in range(1, 9)]
    first = public_view(LernsequenzV1.model_validate(data))
    second = public_view(LernsequenzV1.model_validate(data))
    shown = [item["item_id"] for item in first["checkpoints"][1]["items"]]
    assert shown != [f"s{i}" for i in range(1, 9)] and sorted(shown) == [f"s{i}" for i in range(1, 9)]
    assert shown == [item["item_id"] for item in second["checkpoints"][1]["items"]]
    partners = [item["item_id"] for item in first["checkpoints"][3]["right"]]
    assert sorted(partners) == ["r1", "r2", "r3"]


def test_only_for_is_public() -> None:
    view = public_view(mitmach_sequence())
    assert view["sections"][1]["only_for"] == [{"key": "shell", "option": "powershell"}]
    assert view["checkpoints"][0]["only_for"] == []


# --- grading -------------------------------------------------------------------

def _g(checkpoint_id: str, answer: dict, data: dict | None = None) -> dict:
    return grade(LernsequenzV1.model_validate(data or mitmach_data()), checkpoint_id, answer)


def test_ordering_is_scored_by_position() -> None:
    right = _g("ord", {"order": ["konto", "laden", "anmelden"]})
    assert right["correct"] is True and right["score"] == 1.0
    assert right["correct_order"] == ["konto", "laden", "anmelden"] and right["refs"] == ["c1"]
    swapped = _g("ord", {"order": ["laden", "konto", "anmelden"]})
    assert swapped["correct"] is False and swapped["score"] == pytest.approx(1 / 3)


def test_ordering_with_duplicate_or_missing_ids_is_rejected() -> None:
    for bad in ({"order": ["konto", "konto", "laden"]}, {"order": ["konto", "laden"]},
                {"order": ["konto", "laden", "x"]}, {}):
        with pytest.raises(ValueError):
            _g("ord", bad)


def test_cloze_accepts_typographic_dashes_as_straight_ones() -> None:
    ok = _g("cl", {"values": {"datei": " Install.PS1 "}})
    assert ok["blanks"] == [{"blank_id": "datei", "correct": True}] and ok["score"] == 1.0
    assert _g("cl", {"values": {"datei": "install.cmd"}})["score"] == 0.0
    data = mitmach_data()
    data["checkpoints"][2].update({"template": "claude {{flag}}", "blanks": [{"blank_id": "flag", "accepted": ["--version"]}]})
    assert _g("cl", {"values": {"flag": "—version"}}, data)["score"] == 1.0
    with pytest.raises(ValueError):
        _g("cl", {"values": {"nope": "x"}})


def test_matching_scores_the_fraction_of_right_pairs() -> None:
    assert _g("mt", {"pairs": {"l1": "r1", "l2": "r2", "l3": "r3"}})["correct"] is True
    partial = _g("mt", {"pairs": {"l1": "r1", "l2": "r3", "l3": "r2"}})
    assert partial["score"] == pytest.approx(1 / 3) and partial["pairs"] == {"l1": "r1", "l2": "r2", "l3": "r3"}
    with pytest.raises(ValueError):
        _g("mt", {"pairs": {"l1": "r1"}})


def test_hotspot_hits_when_the_centre_is_inside_and_the_frame_is_tight() -> None:
    hit = _g("hs", {"rect": {"x": 290, "y": 30, "w": 100, "h": 50}})
    assert hit["hit"] is True and hit["score"] == 1.0 and hit["target"] == {"x": 300.0, "y": 40.0, "w": 80.0, "h": 30.0}
    assert _g("hs", {"rect": {"x": 10, "y": 10, "w": 50, "h": 50}})["hit"] is False


def test_hotspot_whole_image_is_wrong() -> None:
    assert _g("hs", {"rect": {"x": 0, "y": 0, "w": 1200, "h": 800}})["hit"] is False


def test_terminal_sim_answers_known_commands_and_knows_the_goal() -> None:
    done = _g("ts", {"command": "claude  --version"})
    assert done["reached"] is True and done["output"] == "2.1.211 (Claude Code)" and done["score"] == 1.0
    other = _g("ts", {"command": "claude doctor"})
    assert other["reached"] is False and other["output"] == "Install health: ok"
    assert _g("ts", {"command": "dir"})["output"].startswith("Der Befehl ist nicht bekannt")
    with pytest.raises(ValueError):
        _g("ts", {"command": ""})


def test_step_done_counts_and_differs_runs_the_check() -> None:
    assert _g("st", {"state": "done"}) == {"kind": "step", "done": True, "score": 1.0, "refs": []}
    differs = _g("st", {"state": "differs", "shell": "powershell", "output": "2.1.211 (Claude Code)"})
    assert differs["kind"] == "command_check" and differs["ok"] is True
    with pytest.raises(ValueError):
        _g("st", {"state": "differs"})


def test_step_differs_without_check_asks_for_the_tutor() -> None:
    data = mitmach_data()
    data["checkpoints"][6]["check"] = None
    result = _g("st", {"state": "differs"}, data)
    assert result["done"] is False and result["score"] == 0.0 and "Tutor" in result["message"]


def test_profile_choices_are_validated_and_echoed() -> None:
    assert _g("pr", {"choices": {"shell": "cmd"}}) == {"kind": "profile", "saved": True, "choices": {"shell": "cmd"}, "refs": []}
    with pytest.raises(ValueError):
        _g("pr", {"choices": {"shell": "fish"}})


# --- Bildaufgaben (Plan 2026-10-27-learning-bildaufgaben, Task 2) ---------------------

def _image_cloze(rect: dict | None = None) -> dict:
    return {"kind": "image_cloze", "checkpoint_id": "ic", "after_section": "f1", "title": "Im Fenster ergänzen",
            "task": "Ergänzen Sie, was im PowerShell-Fenster fehlt.", "image": "/lernsequenz-medien/term-0123456789abcdef.png",
            "width": 760, "height": 96, "shell": "powershell",
            "blanks": [{"blank_id": "wert", "accepted": ["install.ps1"],
                        "rect": rect or {"x": 450.5, "y": 54, "w": 130.1, "h": 22}}]}


def test_image_cloze_validates_and_rejects_rects_outside_the_image() -> None:
    data = sequence_data()
    data["checkpoints"] = [_image_cloze()]
    assert LernsequenzV1.model_validate(data).checkpoints[0].kind == "image_cloze"
    data["checkpoints"] = [_image_cloze({"x": 700, "y": 54, "w": 130, "h": 22})]
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)


def test_public_view_keeps_rects_and_drops_answers() -> None:
    data = sequence_data()
    data["checkpoints"] = [_image_cloze()]
    view = public_view(LernsequenzV1.model_validate(data))["checkpoints"][0]
    assert view["blanks"] == [{"blank_id": "wert", "rect": {"x": 450.5, "y": 54.0, "w": 130.1, "h": 22.0}}]
    assert "install.ps1" not in str(view)


def test_match_items_may_carry_an_image_with_its_size() -> None:
    data = mitmach_data()
    matching = next(c for c in data["checkpoints"] if c["kind"] == "matching")
    for item in matching["left"]:
        item.update({"image": f"/lernsequenz-medien/term-{item['item_id']}000000000000.png", "width": 760, "height": 96})
    assert LernsequenzV1.model_validate(data)
    del matching["left"][0]["width"]
    with pytest.raises(ValidationError):
        LernsequenzV1.model_validate(data)
