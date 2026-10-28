"""Hands-on checkpoints read off a lesson's own structure, without a model.

A numbered list is a sequence to put back in order, a command line is a command with a
blank to fill, a list of ``- **Begriff**: Erklärung`` lines is a matching task. Only what
the lesson literally says is used, so nothing can be made up.
"""

from __future__ import annotations

import os
import re
import secrets

from spaces.learning.contracts.lernsequenz import (
    Blank,
    Checkpoint,
    Cloze,
    Condition,
    ImageBlank,
    ImageCloze,
    MatchItem,
    Matching,
    OrderItem,
    Ordering,
    Profile,
    ProfileOption,
    ProfileQuestion,
    Shell,
)
from spaces.learning.services import zufall
from spaces.learning.services.course_factory.schemas import LessonDraft
from spaces.learning.services.lernsequenz.bilder import render_terminal
from spaces.learning.services.lernsequenz.medien import save_media
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


def _random_ids(prefix: str, count: int) -> list[str]:
    """Ids that say nothing about the authored position: learners can read ids in the page,
    sequential ones (s1, s2 ... or b1->e1) would hand them the solution, and a hash of the
    position could be recomputed from this public code. Drawn once, kept in the stored sequence."""
    ids: list[str] = []
    while len(ids) < count:
        candidate = prefix + secrets.token_hex(3)
        if candidate not in ids:
            ids.append(candidate)
    return ids


def command_lines(body: str) -> list[str]:
    """The command lines a text tells the learner to run (same detection as the checkpoints)."""
    return [block[1] for block in _scan(body) if block[0] == "command" and block[1].startswith(KNOWN_COMMANDS)]


def _ordering(steps: list[str], checkpoint_id: str, after: str, refs: tuple[str, ...]) -> Ordering:
    ids = _random_ids("s", len(steps))
    items = tuple(OrderItem(item_id=ids[i], text=cut(step, 300)) for i, step in enumerate(steps))
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


def _media_available() -> bool:
    """Image tasks need the media folder the player loads from; without it, text tasks only."""
    return bool(os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip())


def _image_cloze(cloze: Cloze) -> ImageCloze:
    """The same blank as ``cloze``, drawn into the shell's window as an empty box."""
    blank = cloze.blanks[0]
    window = render_terminal(cloze.shell, cloze.template, blanks={blank.blank_id: blank.accepted[0]})
    return ImageCloze(
        checkpoint_id=cloze.checkpoint_id, after_section=cloze.after_section, title="Im Fenster ergänzen",
        task=f"Ergänzen Sie, was im Fenster von {SHELL_LABEL[cloze.shell]} fehlt.",
        template=cloze.template, image=save_media(window.png), width=window.width, height=window.height,
        blanks=(ImageBlank(blank_id=blank.blank_id, accepted=blank.accepted, rect=window.rects[blank.blank_id],
                           more=window.more.get(blank.blank_id, ())),),
        shell=cloze.shell, refs=cloze.refs,
    )


def _window_matching(lines: dict[str, str], seed: str, checkpoint_id: str, after: str, refs: tuple[str, ...]) -> Matching:
    """One window per shell (its prompt and this lesson's command, no title bar) to the shell's name."""
    shells = list(lines)
    zufall.ZUFALL.shuffle(shells)
    left_ids, right_ids = _random_ids("f", len(shells)), _random_ids("s", len(shells))
    left = []
    for number, (item_id, shell) in enumerate(zip(left_ids, shells), start=1):
        window = render_terminal(shell, lines[shell], title=False)
        left.append(MatchItem(item_id=item_id, text=f"Fenster {number}", image=save_media(window.png),
                              width=window.width, height=window.height))
    right = [MatchItem(item_id=right_ids[i], text=SHELL_LABEL[shell]) for i, shell in enumerate(shells)]
    right = zufall.anders_gemischt(right, lambda item: item.item_id)
    return Matching(
        checkpoint_id=checkpoint_id, after_section=after, title="Welches Fenster ist welche Shell?",
        task="Ordnen Sie jedem Fenster die Shell zu. Achten Sie auf die Eingabezeile.",
        left=tuple(left), right=tuple(right), pairs=dict(zip(left_ids, right_ids)), refs=refs,
    )


def _matching(pairs: list[tuple[str, str]], seed: str, checkpoint_id: str, after: str, refs: tuple[str, ...]) -> Matching:
    left = tuple(MatchItem(item_id=f"b{i}", text=cut(term, 300)) for i, (term, _text) in enumerate(pairs, start=1))
    right_ids = _random_ids("e", len(pairs))
    right = [MatchItem(item_id=right_ids[i], text=cut(text, 300)) for i, (_term, text) in enumerate(pairs)]
    right = zufall.anders_gemischt(right, lambda item: item.item_id)
    return Matching(
        checkpoint_id=checkpoint_id, after_section=after, title="Zuordnen",
        task="Welche Erklärung gehört zu welchem Begriff?",
        left=left, right=tuple(right),
        pairs={f"b{i}": right_ids[i - 1] for i in range(1, len(pairs) + 1)}, refs=refs,
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
    pictured: set[str] = set()
    first_lines: dict[str, str] = {}
    images = _media_available()

    def next_id(prefix: str) -> str:
        counts[prefix] += 1
        return token(f"{prefix}-{lesson.lesson_id}-{counts[prefix]}", 120)

    for block in _scan(lesson.body):
        if block[0] == "ordering":
            out.append(_ordering(block[1], next_id("ord"), after, refs))
        elif block[0] == "command":
            cloze = _cloze(block[1], block[2], token(f"cl-{lesson.lesson_id}-{counts['cl'] + 1}", 120), after, refs)
            if cloze is not None and "{{" not in block[1]:
                first_lines.setdefault(cloze.shell, block[1])
            # At most MAX_CLOZE per shell: with "Dein Weg" each person sees only their shell's.
            if cloze is not None and per_shell.get(cloze.shell, 0) < MAX_CLOZE:
                counts["cl"] += 1
                per_shell[cloze.shell] = per_shell.get(cloze.shell, 0) + 1
                # The first blank of each shell is drawn into that shell's window.
                if images and cloze.shell not in pictured:
                    pictured.add(cloze.shell)
                    out.append(_image_cloze(cloze))
                else:
                    out.append(cloze)
        else:
            out.append(_matching(block[1], lesson.lesson_id, next_id("mt"), after, refs))
    # Three shells or more in one lesson: which window is which shell?
    if images and len(first_lines) >= 3:
        out.append(_window_matching(first_lines, lesson.lesson_id, next_id("mt"), after, refs))
    return out
