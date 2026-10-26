"""Terminal checks: the pasted output is recognised by fixed rules, errors get the fix for that shell."""

from __future__ import annotations

import json

import pytest

from spaces.learning.contracts.lernsequenz import LernsequenzV1, public_view
from spaces.learning.services.lernsequenz.befehlspruefungen import claude_installiert
from spaces.learning.services.lernsequenz.bewertung import grade
from spaces.learning.tests.unit.test_lernsequenz import sequence_data


def _sequence() -> LernsequenzV1:
    data = sequence_data()
    data["checkpoints"] = [claude_installiert("ci", data["sections"][0]["section_id"]).model_dump(mode="json")]
    return LernsequenzV1.model_validate(data)


def _grade(output: str, shell: str) -> dict:
    return grade(_sequence(), "ci", {"output": output, "shell": shell})


def test_a_version_means_installed_in_every_shell() -> None:
    for shell in ("powershell", "cmd", "unix"):
        result = _grade("2.1.211 (Claude Code)\n", shell)
        assert result["ok"] is True and result["recognized"] is True and result["fix"] == []


def test_not_found_gets_the_path_fix_of_that_shell_also_in_german() -> None:
    ps = _grade("claude : The term 'claude' is not recognized as the name of a cmdlet, function", "powershell")
    assert ps["ok"] is False and ps["recognized"] is True
    assert ps["fix"] and all(f["shell"] == "powershell" for f in ps["fix"])
    assert "SetEnvironmentVariable" in ps["fix"][-1]["command"]

    ps_de = _grade('claude : Die Benennung "claude" wurde nicht als Name eines Cmdlet erkannt.', "powershell")
    assert ps_de["fix"] == ps["fix"]

    cmd_de = _grade('Der Befehl "claude" ist entweder falsch geschrieben oder konnte nicht gefunden werden.', "cmd")
    assert cmd_de["recognized"] and "findstr" in cmd_de["fix"][-1]["command"]

    unix = _grade("zsh: command not found: claude", "unix")
    assert any(".zshrc" in f["command"] for f in unix["fix"])


def test_the_wrong_shell_tab_still_says_what_to_do_and_unknown_output_asks_for_more() -> None:
    wrong = _grade("'claude' is not recognized as an internal or external command", "powershell")
    assert wrong["recognized"] is True and wrong["fix"] == [] and "Shell" in wrong["message"]
    unknown = _grade("irgendwas anderes", "unix")
    assert unknown["recognized"] is False and "claude doctor" in unknown["message"]


def test_typographic_dash_is_recognised_with_the_right_command() -> None:
    r = _grade("The term '—version' is not recognized", "powershell")
    assert r["recognized"] is True and r["ok"] is False
    assert r["fix"] == [{"shell": "powershell", "command": "claude --version"}]
    assert "Gedankenstrich" in r["message"]
    quotes = _grade("Unexpected token “version”", "unix")
    assert "Anführungszeichen" in quotes["message"]


def test_pasting_the_command_itself_is_told_apart_from_its_output() -> None:
    assert "nicht seine Ausgabe" in _grade("claude --version", "cmd")["message"]
    assert "nicht seine Ausgabe" in _grade("  claude --version  \n", "unix")["message"]


def test_secrets_in_output_raise_a_warning_and_are_not_echoed() -> None:
    r = _grade("2.1.211 (Claude Code)\nANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz0123", "unix")
    assert r["ok"] is True and "Schlüssel" in r["warning"] and "sk-ant" not in json.dumps(r, ensure_ascii=False)
    assert "warning" not in _grade("2.1.211 (Claude Code)", "unix")


def test_rules_stay_on_the_server_and_bad_answers_are_refused() -> None:
    checkpoint = public_view(_sequence())["checkpoints"][0]
    assert "success" not in checkpoint and "problems" not in checkpoint
    assert [c["shell"] for c in checkpoint["commands"]] == ["powershell", "cmd", "unix"]
    with pytest.raises(ValueError):
        _grade("   ", "unix")
    with pytest.raises(ValueError):
        _grade("2.1.0 (Claude Code)", "fish")
