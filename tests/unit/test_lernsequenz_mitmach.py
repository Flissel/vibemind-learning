"""Hands-on checkpoints: ordering, cloze, matching, hotspot, terminal sim, step, profile."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spaces.learning.contracts.lernsequenz import LernsequenzV1, public_view
from spaces.learning.services.lernsequenz.befehlspruefungen import claude_installiert
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


def test_only_for_is_public() -> None:
    view = public_view(mitmach_sequence())
    assert view["sections"][1]["only_for"] == [{"key": "shell", "option": "powershell"}]
    assert view["checkpoints"][0]["only_for"] == []
