"""Hands-on checkpoints read off a lesson's own structure, without a model.

A numbered list is a sequence to put back in order, a command line is a command with a
blank to fill, a list of ``- **Begriff**: Erklärung`` lines is a matching task. Only what
the lesson literally says is used, so nothing can be made up.
"""

from __future__ import annotations

import random
import re

from spaces.learning.contracts.lernsequenz import (
    Blank,
    Checkpoint,
    Cloze,
    Condition,
    MatchItem,
    Matching,
    OrderItem,
    Ordering,
    Profile,
    ProfileOption,
    ProfileQuestion,
    Shell,
)
from spaces.learning.services.course_factory.schemas import LessonDraft
from spaces.learning.services.lernsequenz.text import cut, plain, token

# A command line only counts when it starts like one of the commands the courses teach.
KNOWN_COMMANDS = (
    "irm ", "curl ", "claude", "brew ", "winget ", "npm ", "echo ", "Test-Path", "dir ", "ls ",
    "source ", "$env:", "[Environment]",
)
MAX_CLOZE = 3
SHELL_LABEL: dict[Shell, str] = {"powershell": "Windows PowerShell", "cmd": "Windows CMD", "unix": "macOS / Linux"}

_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_PAIR = re.compile(r"^\s*[-*+]\s+(?:\*\*([^*:]{1,60})\*\*|([^:*`]{1,60})):\s+(.+)$")
_FENCE = re.compile(r"^\s*```\s*([A-Za-z0-9_-]*)\s*$")
_INLINE = re.compile(r"^\s*`([^`]+)`\s*$")
_FENCE_SHELLS: dict[str, Shell] = {
    "powershell": "powershell", "pwsh": "powershell", "ps1": "powershell",
    "cmd": "cmd", "bat": "cmd", "batch": "cmd",
    "bash": "unix", "sh": "unix", "zsh": "unix", "shell": "unix",
}


def _shell_of(line: str, fence: str = "") -> Shell:
    if fence in _FENCE_SHELLS:
        return _FENCE_SHELLS[fence]
    if line.startswith(("irm ", "$env:", "Test-Path", "[Environment]")):
        return "powershell"
    if (line.startswith("curl ") and "install.cmd" in line) or line.startswith("dir ") or "findstr" in line:
        return "cmd"
    return "unix"


def _scan(body: str) -> list[tuple]:
    """Blocks in reading order: ("ordering", [steps]), ("command", line, fence), ("matching", [(term, text)])."""
    found: list[tuple] = []
    fence: str | None = None
    numbered: list[str] = []
    pairs: list[tuple[str, str]] = []

    def flush() -> None:
        nonlocal numbered, pairs
        if 3 <= len(numbered) <= 8:
            found.append(("ordering", numbered))
        if len(pairs) >= 3:
            found.append(("matching", pairs[:8]))
        numbered, pairs = [], []

    for raw in body.splitlines():
        opened = _FENCE.match(raw)
        if opened:
            flush()
            fence = None if fence is not None else opened.group(1).lower()
            continue
        if fence is not None:
            if raw.strip():
                found.append(("command", raw.strip(), fence))
            continue
        step = _NUMBERED.match(raw)
        if step:
            if pairs:
                flush()
            numbered.append(plain(step.group(1)))
            continue
        pair = _PAIR.match(raw)
        if pair:
            if numbered:
                flush()
            pairs.append((plain(pair.group(1) or pair.group(2)), plain(pair.group(3))))
            continue
        flush()
        if raw.startswith(("    ", "\t")):
            found.append(("command", raw.strip(), ""))
        elif (inline := _INLINE.match(raw)):
            found.append(("command", inline.group(1).strip(), ""))
    flush()
    return found


def _ordering(steps: list[str], checkpoint_id: str, after: str, refs: tuple[str, ...]) -> Ordering:
    items = tuple(OrderItem(item_id=f"s{i}", text=cut(step, 300)) for i, step in enumerate(steps, start=1))
    return Ordering(
        checkpoint_id=checkpoint_id, after_section=after, title="Reihenfolge",
        task="Bringen Sie die Schritte in die richtige Reihenfolge.",
        items=items, correct=tuple(item.item_id for item in items), refs=refs,
    )


def _cloze(line: str, fence: str, checkpoint_id: str, after: str, refs: tuple[str, ...]) -> Cloze | None:
    if not line.startswith(KNOWN_COMMANDS):
        return None
    tokens = line.split()
    candidates = [(i, t) for i, t in enumerate(tokens) if "/" in t or "." in t]
    index, chosen = max(candidates, key=lambda c: len(c[1])) if candidates else (len(tokens) - 1, tokens[-1])
    # A URL keeps its address; only the file name at its end is the blank.
    blank = chosen.rsplit("/", 1)[-1] if "://" in chosen else chosen
    if not blank or len(blank) > 200:
        return None
    tokens[index] = chosen[: len(chosen) - len(blank)] + "{{wert}}"
    template = " ".join(tokens)
    if len(template) > 500:
        return None
    shell = _shell_of(line, fence)
    return Cloze(
        checkpoint_id=checkpoint_id, after_section=after, title="Befehl vervollständigen",
        task=f"Ergänzen Sie den Befehl für {SHELL_LABEL[shell]}.",
        template=template, blanks=(Blank(blank_id="wert", accepted=(blank,)),), shell=shell, refs=refs,
    )


def _matching(pairs: list[tuple[str, str]], seed: str, checkpoint_id: str, after: str, refs: tuple[str, ...]) -> Matching:
    left = tuple(MatchItem(item_id=f"b{i}", text=cut(term, 300)) for i, (term, _text) in enumerate(pairs, start=1))
    right = [MatchItem(item_id=f"e{i}", text=cut(text, 300)) for i, (_term, text) in enumerate(pairs, start=1)]
    random.Random(seed).shuffle(right)
    return Matching(
        checkpoint_id=checkpoint_id, after_section=after, title="Zuordnen",
        task="Welche Erklärung gehört zu welchem Begriff?",
        left=left, right=tuple(right),
        pairs={f"b{i}": f"e{i}" for i in range(1, len(pairs) + 1)}, refs=refs,
    )


# --- "Dein Weg" -------------------------------------------------------------------

_SHELL_WORDS: tuple[tuple[Shell, tuple[str, ...]], ...] = (
    ("powershell", ("powershell",)),
    ("cmd", ("cmd", "eingabeaufforderung")),
    ("unix", ("macos", "linux", "wsl")),
)


def shell_condition(title: str) -> tuple[Condition, ...]:
    """The shell a slide title names, as its ``only_for``; nothing when it names none."""
    lowered = title.lower()
    for shell, words in _SHELL_WORDS:
        if any(word in lowered for word in words):
            return (Condition(key="shell", option=shell),)
    return ()


def profile_checkpoint(after: str) -> Profile:
    """The one profile question the setup courses need: which shell the person uses."""
    return Profile(
        checkpoint_id="dein-weg", after_section=after, title="Dein Weg",
        questions=(ProfileQuestion(key="shell", question="Womit arbeiten Sie?", options=(
            ProfileOption(option_id="powershell", text="Windows PowerShell (Eingabezeile beginnt mit PS)"),
            ProfileOption(option_id="cmd", text="Windows Eingabeaufforderung (CMD)"),
            ProfileOption(option_id="unix", text="macOS, Linux oder WSL"),
        )),),
    )


def structured_checkpoints(lesson: LessonDraft, *, after: str, refs: tuple[str, ...]) -> list[Checkpoint]:
    """The lesson's lists and command lines as checkpoints after section ``after``."""
    out: list[Checkpoint] = []
    counts = {"ord": 0, "cl": 0, "mt": 0}

    def next_id(prefix: str) -> str:
        counts[prefix] += 1
        return token(f"{prefix}-{lesson.lesson_id}-{counts[prefix]}", 120)

    for block in _scan(lesson.body):
        if block[0] == "ordering":
            out.append(_ordering(block[1], next_id("ord"), after, refs))
        elif block[0] == "command":
            if counts["cl"] >= MAX_CLOZE:
                continue
            cloze = _cloze(block[1], block[2], token(f"cl-{lesson.lesson_id}-{counts['cl'] + 1}", 120), after, refs)
            if cloze is not None:
                counts["cl"] += 1
                out.append(cloze)
        else:
            out.append(_matching(block[1], lesson.lesson_id, next_id("mt"), after, refs))
    return out
