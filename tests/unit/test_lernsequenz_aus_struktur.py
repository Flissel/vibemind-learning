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
        ("cmd", "install.cmd"), ("unix", "install.sh"), ("powershell", "\"$env:USERPROFILE\\.local\\bin\"")]


def test_at_most_three_cloze_per_lesson_and_unknown_commands_are_skipped() -> None:
    body = "# T\n\n" + "".join(f"    claude --flag{i}\n\n" for i in range(5)) + "    rm -rf /\n"
    checkpoints = structured_checkpoints(lesson(body), after="l1-1", refs=())
    assert len(checkpoints) == 3 and all(c.kind == "cloze" for c in checkpoints)
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
