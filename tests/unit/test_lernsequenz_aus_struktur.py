"""Hands-on checkpoints read off a lesson's own structure, without a model."""

from __future__ import annotations

from spaces.learning.services.course_factory.schemas import CourseDraft, LessonDraft
from spaces.learning.services.lernsequenz.aus_kurs import build_presentation
from spaces.learning.services.lernsequenz.aus_struktur import structured_checkpoints
from spaces.learning.tests.unit.test_lernsequenz_aus_kurs import CIT, COURSE, draft

BODY = """# Claude Code installieren

Die Installation dauert zwei Minuten.

## Ablauf

1. Konto bei Anthropic anlegen
2. Installer herunterladen
3. Installation starten
4. Im Terminal anmelden

## Befehl für PowerShell

Kopieren Sie diese Zeile:

`irm https://claude.ai/install.ps1 | iex`

## Prüfen

    claude --version

## Begriffe

- **Terminal**: Das Fenster, in dem Befehle eingetippt werden.
- **PATH**: Die Liste der Ordner, in denen Programme gesucht werden.
- **Installer**: Das Programm, das Claude Code einrichtet.
"""


def lesson(body: str = BODY) -> LessonDraft:
    return LessonDraft.model_validate({
        "lesson_id": "l1", "chapter_id": "k1", "title": "Claude Code installieren", "body": body,
        "concept_ids": ["pruefen"], "claims": [{"claim_id": "c1", "text": "Prüfen.", "citation_ids": [CIT]}],
    })


def test_numbered_list_commands_and_terms_become_checkpoints() -> None:
    checkpoints = structured_checkpoints(lesson(), after="l1-4", refs=(CIT,))

    assert [c.kind for c in checkpoints] == ["ordering", "cloze", "cloze", "matching"]
    assert all(c.after_section == "l1-4" and c.refs == (CIT,) for c in checkpoints)
    ordering = checkpoints[0]
    assert ordering.checkpoint_id == "ord-l1-1"
    assert [i.text for i in ordering.items] == ["Konto bei Anthropic anlegen", "Installer herunterladen",
                                                "Installation starten", "Im Terminal anmelden"]
    assert ordering.correct == tuple(i.item_id for i in ordering.items)
    first, second = checkpoints[1], checkpoints[2]
    assert first.template == "irm https://claude.ai/{{wert}} | iex" and first.blanks[0].accepted == ("install.ps1",)
    assert first.shell == "powershell"
    assert second.template == "claude {{wert}}" and second.blanks[0].accepted == ("--version",) and second.shell == "unix"
    matching = checkpoints[3]
    assert [i.text for i in matching.left] == ["Terminal", "PATH", "Installer"]
    assert {i.text for i in matching.right} == {"Das Fenster, in dem Befehle eingetippt werden.",
                                                "Die Liste der Ordner, in denen Programme gesucht werden.",
                                                "Das Programm, das Claude Code einrichtet."}
    right = {i.item_id: i.text for i in matching.right}
    assert right[matching.pairs[matching.left[1].item_id]].startswith("Die Liste")


def test_fenced_code_and_known_shells() -> None:
    body = ("# T\n\n```cmd\ncurl -fsSL https://claude.ai/install.cmd -o install.cmd\n```\n\n"
            "```bash\ncurl -fsSL https://claude.ai/install.sh | bash\n```\n\n    Test-Path \"$env:USERPROFILE\\.local\\bin\"\n")
    checkpoints = structured_checkpoints(lesson(body), after="l1-1", refs=())
    assert [(c.shell, c.blanks[0].accepted[0]) for c in checkpoints] == [
        ("cmd", "install.cmd"), ("unix", "install.sh"), ("powershell", "$env:USERPROFILE\\.local\\bin")]
    assert checkpoints[2].template == 'Test-Path "{{wert}}"'


def test_at_most_three_cloze_per_lesson_and_shell_and_unknown_commands_are_skipped() -> None:
    body = "# T\n\n" + "".join(f"    claude --flag{i}\n\n" for i in range(5)) + "    rm -rf /\n\n    Test-Path claude.exe\n"
    checkpoints = structured_checkpoints(lesson(body), after="l1-1", refs=())
    assert [c.shell for c in checkpoints] == ["unix", "unix", "unix", "powershell"]
    assert "rm" not in "".join(c.template for c in checkpoints)


def test_short_lists_and_prose_produce_nothing() -> None:
    body = "# T\n\nNur Text.\n\n1. eins\n2. zwei\n\n- **A**: a\n- **B**: b\n\n- ohne Doppelpunkt\n- auch nicht\n- und nicht\n"
    assert structured_checkpoints(lesson(body), after="l1-1", refs=()) == []


def test_build_presentation_places_structure_before_quizzes() -> None:
    data = draft().model_dump(mode="json")
    data["lessons"][0]["body"] = BODY
    sequence = build_presentation(CourseDraft.model_validate(data), course_id=COURSE)

    kinds = [c.kind for c in sequence.checkpoints]
    assert kinds == ["ordering", "cloze", "cloze", "matching", "choice"]
    lesson_last = [s.section_id for s in sequence.sections][-1]
    assert all(c.after_section == lesson_last for c in sequence.checkpoints)
    assert sequence.checkpoints[0].refs == (CIT,)


# --- "Dein Weg" -------------------------------------------------------------------

def test_profile_adds_the_shell_question_and_tags_slides() -> None:
    from spaces.learning.contracts.lernsequenz import Condition
    from spaces.learning.services.lernsequenz.aus_struktur import profile_checkpoint, shell_condition

    powershell = (Condition(key="shell", option="powershell"),)
    assert shell_condition("Schritt 2: Installieren in Windows PowerShell") == powershell
    assert shell_condition("Eingabeaufforderung (CMD) einrichten") == (Condition(key="shell", option="cmd"),)
    assert shell_condition("Unter macOS oder Linux") == (Condition(key="shell", option="unix"),)
    assert shell_condition("Schritt 3: Git für Windows") == ()
    profile = profile_checkpoint(after="l1-1")
    assert profile.kind == "profile" and profile.after_section == "l1-1" and profile.questions[0].key == "shell"
    assert [o.option_id for o in profile.questions[0].options] == ["powershell", "cmd", "unix"]

    data = draft().model_dump(mode="json")
    data["lessons"][0]["body"] = (
        "# Claude Code installieren\n\nKurz.\n\n## Schritt 2: Installieren in Windows PowerShell\n\n"
        "- Zeile kopieren\n- Einfügen\n\n`irm https://claude.ai/install.ps1 | iex`\n\n"
        "## Schritt 3: Git für Windows\n\n- Installer laden\n"
    )
    sequence = build_presentation(CourseDraft.model_validate(data), course_id=COURSE, profile=True)
    assert sequence.checkpoints[0].kind == "profile"
    assert sequence.checkpoints[0].after_section == sequence.sections[0].section_id
    by_title = {s.title: s.only_for for s in sequence.sections}
    assert by_title["Schritt 2: Installieren in Windows PowerShell"] == powershell
    assert by_title["Schritt 3: Git für Windows"] == () and by_title["Claude Code installieren"] == ()
    cloze = next(c for c in sequence.checkpoints if c.kind == "cloze")
    assert cloze.only_for == powershell
    plain = build_presentation(CourseDraft.model_validate(data), course_id=COURSE)
    assert all(c.kind != "profile" for c in plain.checkpoints) and all(s.only_for == () for s in plain.sections)


def test_cli_praesentation_takes_a_profile(monkeypatch, tmp_path) -> None:
    from spaces.learning.services.lernsequenz import cli

    data = draft().model_dump(mode="json")
    data["lessons"][0]["body"] = BODY
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "published_draft", lambda course_id: CourseDraft.model_validate(data))
    monkeypatch.setattr(cli, "service_key", lambda: "k" * 32, raising=False)
    cli.main(["praesentation", "--course", COURSE, "--profil", "shell"])
    saved = next(tmp_path.rglob("praes-*.json"))
    import json
    assert json.loads(saved.read_text(encoding="utf-8"))["checkpoints"][0]["kind"] == "profile"


# --- what the real setup courses looked like (27.10.) ------------------------------

def test_plain_command_lines_count_but_sentences_do_not() -> None:
    body = ("# T\n\ncurl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd\n\n"
            "Test-Path \"$env:USERPROFILE\\.local\\bin\\claude.exe\"\n\n"
            "claude --version prüft danach die Installation.\n")
    checkpoints = structured_checkpoints(lesson(body), after="l1-1", refs=())
    assert [(c.shell, c.blanks[0].accepted[0]) for c in checkpoints] == [
        ("cmd", "install.cmd"), ("powershell", "$env:USERPROFILE\\.local\\bin\\claude.exe")]
    # A blank that recurs in the line is blanked everywhere, or it would give itself away.
    assert checkpoints[0].template == "curl -fsSL https://claude.ai/{{wert}} -o {{wert}} && {{wert}} && del {{wert}}"


def test_quotes_around_the_blank_stay_in_the_line() -> None:
    body = "# T\n\n    echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc\n"
    cloze = structured_checkpoints(lesson(body), after="l1-1", refs=())[0]
    assert cloze.template == "echo 'export {{wert}}' >> ~/.zshrc"
    assert cloze.blanks[0].accepted == ('PATH="$HOME/.local/bin:$PATH"',)


def test_pairs_whose_text_is_a_link_make_no_matching() -> None:
    body = ("# T\n\n- **Windows (x64)**: https://claude.ai/api/desktop/win32/x64\n"
            "- **macOS**: https://claude.ai/api/desktop/darwin\n- **Linux**: https://claude.ai/api/desktop/linux\n")
    assert structured_checkpoints(lesson(body), after="l1-1", refs=()) == []


def test_titles_naming_several_shells_apply_to_all() -> None:
    from spaces.learning.services.lernsequenz.aus_struktur import shell_condition

    assert shell_condition("Schritt 1: Die eigene Shell erkennen (PowerShell, CMD, macOS)") == ()
    assert shell_condition("PATH reparieren je Shell: PowerShell, CMD, macOS und Linux") == ()
    assert shell_condition("Andere Installationswege: Homebrew, WinGet und Linux-Paketverwaltung") == ()
    assert shell_condition("Installieren mit Homebrew")[0].option == "unix"


def test_flags_and_trailing_commas_are_never_the_blank() -> None:
    body = ("# T\n\n    echo %PATH% | findstr /i \"local\\bin\"\n\n"
            "    [Environment]::SetEnvironmentVariable('PATH', \"$currentPath;$env:USERPROFILE\\.local\\bin\", 'User')\n")
    first, second = structured_checkpoints(lesson(body), after="l1-1", refs=())
    assert first.template == 'echo %PATH% | findstr /i "{{wert}}"' and first.blanks[0].accepted == ("local\\bin",)
    assert second.template == "[Environment]::SetEnvironmentVariable('PATH', \"{{wert}}\", 'User')"
    assert second.blanks[0].accepted == ("$currentPath;$env:USERPROFILE\\.local\\bin",)


def test_item_ids_do_not_reveal_the_solution() -> None:
    # A learner can read ids in the page: they must not sort into the answer.
    checkpoints = structured_checkpoints(lesson(), after="l1-4", refs=(CIT,))
    ordering, matching = checkpoints[0], checkpoints[3]
    # Random, not numbered and not a hash of the position (the code is public): the id carries
    # no position and no partner index, and two builds never draw the same ids.
    positions = {f"s{i}" for i in range(1, 9)} | {f"e{i}" for i in range(1, 9)}
    assert all(i.item_id not in positions and len(i.item_id) == 7 for i in ordering.items)
    assert all(r.item_id not in positions and len(r.item_id) == 7 for r in matching.right)
    assert all(matching.pairs[l.item_id] != l.item_id.replace("b", "e") for l in matching.left)
    other = structured_checkpoints(lesson(), after="l1-4", refs=(CIT,))
    assert [i.item_id for i in ordering.items] != [i.item_id for i in other[0].items]
    assert len({i.item_id for i in ordering.items}) == len(ordering.items)


# --- Bildaufgaben (Plan 2026-10-27-learning-bildaufgaben, Task 3) ----------------------

import pytest  # noqa: E402

THREE_SHELLS = """# Installieren

```powershell
irm https://claude.ai/install.ps1 | iex
```

```cmd
curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd
```

```bash
curl -fsSL https://claude.ai/install.sh | bash
```
"""


@pytest.fixture()
def media(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    return tmp_path / "lernsequenzen" / "medien"


def test_the_first_command_of_each_shell_becomes_an_image_cloze(media) -> None:
    checkpoints = structured_checkpoints(lesson(), after="l1-4", refs=(CIT,))

    images = [c for c in checkpoints if c.kind == "image_cloze"]
    assert {c.shell for c in images} == {"powershell", "unix"}
    ps = next(c for c in images if c.shell == "powershell")
    assert ps.blanks[0].accepted == ("install.ps1",) and ps.title == "Im Fenster ergänzen"
    assert (media / ps.image.rsplit("/", 1)[-1]).is_file()
    assert "Ergänzen Sie" in ps.task and ps.refs == (CIT,)


def test_image_cloze_and_text_cloze_never_share_a_line(media) -> None:
    body = BODY.replace("`irm https://claude.ai/install.ps1 | iex`",
                        "```powershell\nirm https://claude.ai/install.ps1 | iex\nTest-Path \"$env:USERPROFILE\.local\bin\claude.exe\"\n```")
    checkpoints = structured_checkpoints(lesson(body), after="l1-4", refs=(CIT,))

    image = next(c for c in checkpoints if c.kind == "image_cloze" and c.shell == "powershell")
    text = [c for c in checkpoints if c.kind == "cloze" and c.shell == "powershell"]
    assert image.blanks[0].accepted == ("install.ps1",)
    assert text and all("install.ps1" not in b.accepted for c in text for b in c.blanks)


def test_three_shells_give_one_window_matching_without_titles(media) -> None:
    checkpoints = structured_checkpoints(lesson(THREE_SHELLS), after="l1-1", refs=(CIT,))

    [windows] = [c for c in checkpoints if c.kind == "matching" and c.title == "Welches Fenster ist welche Shell?"]
    assert all(item.image and item.width == 560 for item in windows.left)
    assert {item.text for item in windows.right} == {"Windows PowerShell", "Windows CMD", "macOS / Linux"}
    by_id = {item.item_id: item.text for item in windows.right}
    labels = [by_id[windows.pairs[item.item_id]] for item in windows.left]
    assert sorted(labels) == sorted(["Windows PowerShell", "Windows CMD", "macOS / Linux"])
    assert not any(item.item_id.startswith(("b1", "s1")) for item in windows.left)


def test_two_shells_give_no_window_matching(media) -> None:
    body = THREE_SHELLS.split("```bash")[0]
    checkpoints = structured_checkpoints(lesson(body), after="l1-1", refs=(CIT,))

    assert not any(c.kind == "matching" and c.title == "Welches Fenster ist welche Shell?" for c in checkpoints)


def test_without_a_media_folder_there_are_no_image_tasks(monkeypatch) -> None:
    monkeypatch.delenv("LEARNING_ARTIFACT_ROOT", raising=False)

    kinds = [c.kind for c in structured_checkpoints(lesson(THREE_SHELLS), after="l1-1", refs=(CIT,))]

    assert "image_cloze" not in kinds and kinds.count("cloze") == 3
