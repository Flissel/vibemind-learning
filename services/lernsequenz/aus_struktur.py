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
# A bare line counts as a command when it starts like one and does not read like a sentence.
_MAX_COMMAND_TOKENS = 16
_SENTENCE_END = (".", ":", "!", "?", ",")
_QUOTES = "\"'"
_FLAG = re.compile(r"^[-/][A-Za-z]{1,2}$")
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


def _bare(t: str) -> str:
    """The token without the quotes that belong to the line: a wrapping pair, and a trailing
    quote that has no partner inside the token (``'export PATH="…"'``)."""
    t = t.rstrip(",;")
    for q in _QUOTES:
        if len(t) > 1 and t[0] == q and t[-1] == q:
            t = t[1:-1]
    while t and t[-1] in _QUOTES and t.count(t[-1]) % 2 == 1:
        t = t[:-1]
    return t


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
        # A term whose "explanation" is a link (download lists) is nothing to match.
        if pair and not pair.group(3).strip().startswith(("http://", "https://")):
            if numbered:
                flush()
            pairs.append((plain(pair.group(1) or pair.group(2)), plain(pair.group(3))))
            continue
        flush()
        line = raw.strip()
        if raw.startswith(("    ", "\t")):
            found.append(("command", line, ""))
        elif (inline := _INLINE.match(raw)):
            found.append(("command", inline.group(1).strip(), ""))
        elif line.startswith(KNOWN_COMMANDS) and not line.endswith(_SENTENCE_END) and len(line.split()) <= _MAX_COMMAND_TOKENS:
            found.append(("command", line, ""))
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
    candidates = [t for t in tokens if ("/" in t or "." in t) and not _FLAG.match(t)]
    chosen = max(candidates, key=len) if candidates else tokens[-1]
    # A URL keeps its address; only the file name at its end is the blank. Quotes around
    # the value belong to the line, not to the answer.
    blank = _bare(chosen.rsplit("/", 1)[-1] if "://" in chosen else chosen)
    if not blank or len(blank) > 200:
        return None

    def blanked(t: str) -> str:
        # Everywhere it occurs, or the rest of the line would give the answer away.
        core = _bare(t)
        if core == blank or ("://" in core and core.endswith("/" + blank)):
            return t.replace(blank, "{{wert}}", 1) if core == blank else t[: t.rfind(blank)] + "{{wert}}" + t[t.rfind(blank) + len(blank):]
        return t

    template = " ".join(blanked(t) for t in tokens)
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

_SHELL_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("powershell", ("powershell",)),
    ("cmd", ("cmd", "eingabeaufforderung")),
    ("unix", ("macos", "linux", "wsl", "homebrew", "brew")),
    # Windows without a shell (WinGet): no condition of its own, but a second family.
    ("windows", ("winget",)),
)


def shell_condition(title: str) -> tuple[Condition, ...]:
    """The one shell a slide title names, as its ``only_for``; a title naming none or
    several ("PowerShell, CMD, macOS") applies to everyone."""
    lowered = title.lower()
    named = [shell for shell, words in _SHELL_WORDS if any(word in lowered for word in words)]
    if len(named) != 1 or named[0] == "windows":
        return ()
    return (Condition(key="shell", option=named[0]),)


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
    per_shell: dict[str, int] = {}

    def next_id(prefix: str) -> str:
        counts[prefix] += 1
        return token(f"{prefix}-{lesson.lesson_id}-{counts[prefix]}", 120)

    for block in _scan(lesson.body):
        if block[0] == "ordering":
            out.append(_ordering(block[1], next_id("ord"), after, refs))
        elif block[0] == "command":
            cloze = _cloze(block[1], block[2], token(f"cl-{lesson.lesson_id}-{counts['cl'] + 1}", 120), after, refs)
            # At most MAX_CLOZE per shell: with "Dein Weg" each person sees only their shell's.
            if cloze is not None and per_shell.get(cloze.shell, 0) < MAX_CLOZE:
                counts["cl"] += 1
                per_shell[cloze.shell] = per_shell.get(cloze.shell, 0) + 1
                out.append(cloze)
        else:
            out.append(_matching(block[1], lesson.lesson_id, next_id("mt"), after, refs))
    return out
