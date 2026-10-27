"""Terminal windows drawn from the lesson's own command line.

No image model: every character comes from the verified lesson text, drawn in a fixed-width
font, so the window shows exactly the command the lesson teaches and the position of every
blank is known to the pixel. A blank is drawn as an empty box; its answer is never drawn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from spaces.learning.contracts.lernsequenz import Rect, Shell

# 560 px: still readable when a phone scales the window to ~310 px.
WIDTH = 560
FONT_SIZE = 18
LINE = 26
PAD = 18
TITLE = 34
BOX_FILL = (0xFF, 0xF4, 0xC2)
BOX_BORDER = (0xC9, 0x9A, 0x00)
_TITLE_BG = (0xE6, 0xE6, 0xE6)
_TITLE_FG = (0x33, 0x33, 0x33)
_MIN_BOX_CELLS = 4
_FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "DejaVuSansMono.ttf"
_PLACEHOLDER = re.compile(r"\{\{([A-Za-z0-9_-]+)\}\}")
_SPACE = ("text", " ")


@dataclass(frozen=True)
class _Style:
    title: str
    prompt: str
    background: tuple[int, int, int]
    foreground: tuple[int, int, int]


_STYLES: dict[str, _Style] = {
    "powershell": _Style("Windows PowerShell", "PS C:\\Users\\Name> ", (0x01, 0x24, 0x56), (0xEE, 0xED, 0xF0)),
    "cmd": _Style("Eingabeaufforderung", "C:\\Users\\Name>", (0x0C, 0x0C, 0x0C), (0xCC, 0xCC, 0xCC)),
    # The contract does not tell macOS from Linux; the window looks like the macOS Terminal.
    "unix": _Style("Terminal", "name@Mac ~ % ", (0x1E, 0x1E, 0x1E), (0xE6, 0xE6, 0xE6)),
}


@dataclass(frozen=True)
class RenderedWindow:
    png: bytes
    width: int
    height: int
    rects: dict[str, Rect]
    char_width: float
    # Further boxes of a blank that occurs more than once (they show what is typed in the first).
    more: dict[str, tuple[Rect, ...]] = field(default_factory=dict)


@lru_cache(maxsize=1)
def _font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT), FONT_SIZE)


def _char_width() -> float:
    return _font().getlength("M")


def _columns() -> int:
    return int((WIDTH - 2 * PAD) // _char_width())


def _units(command: str, blanks: dict[str, str]) -> list[tuple[str, str]]:
    """("text", one character) and ("blank", blank_id) in reading order."""
    if set(_PLACEHOLDER.findall(command)) != set(blanks):
        raise ValueError("placeholders and blanks must match")
    units: list[tuple[str, str]] = []
    position = 0
    for match in _PLACEHOLDER.finditer(command):
        units += [("text", ch) for ch in command[position:match.start()]]
        units.append(("blank", match.group(1)))
        position = match.end()
    units += [("text", ch) for ch in command[position:]]
    return units


def _layout(shell: Shell, command: str, blanks: dict[str, str]) -> tuple[list[tuple[str, str, int, int, int]], int]:
    """(kind, value, row, column, cells) per unit and the number of rows. Lines break at spaces;
    a word longer than a line breaks between characters; a box never splits."""
    columns = _columns()
    units = [("text", ch) for ch in _STYLES[shell].prompt] + _units(command, blanks)

    def cells(unit: tuple[str, str]) -> int:
        if unit[0] == "blank":
            return min(max(len(blanks[unit[1]]), _MIN_BOX_CELLS) + 1, columns)
        return 1

    words: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for unit in units:
        if unit == _SPACE:
            if current:
                words.append(current)
                current = []
            words.append([unit])
        else:
            current.append(unit)
    if current:
        words.append(current)

    placed: list[tuple[str, str, int, int, int]] = []
    row = column = 0
    for word in words:
        if word == [_SPACE]:
            if column == 0:
                continue
            if column + 1 > columns:
                row, column = row + 1, 0
                continue
            placed.append(("text", " ", row, column, 1))
            column += 1
            continue
        width = sum(cells(unit) for unit in word)
        if column > 0 and column + width > columns and width <= columns:
            row, column = row + 1, 0
        for unit in word:
            size = cells(unit)
            if column + size > columns:
                row, column = row + 1, 0
            placed.append((unit[0], unit[1], row, column, size))
            column += size
    return placed, row + 1


def wrapped_lines(shell: Shell, command: str, blanks: dict[str, str] | None = None) -> list[str]:
    """The window's text lines as laid out (boxes as underscores), for checks and alt texts."""
    placed, rows = _layout(shell, command, blanks or {})
    lines = [""] * rows
    for kind, value, row, _column, size in placed:
        lines[row] += value if kind == "text" else "_" * size
    return [line.rstrip() for line in lines]


def render_terminal(
    shell: Shell, command: str, *, blanks: dict[str, str] | None = None, title: bool = True
) -> RenderedWindow:
    """The window of ``shell`` with its prompt and ``command``; ``{{id}}`` placeholders become
    empty boxes as wide as their answer in ``blanks`` (the answer itself is never drawn).
    ``title=False`` leaves the title bar empty (it would name the shell in a matching task)."""
    style = _STYLES[shell]
    blanks = blanks or {}
    font = _font()
    char = _char_width()
    placed, rows = _layout(shell, command, blanks)
    height = TITLE + 2 * PAD + rows * LINE

    image = Image.new("RGB", (WIDTH, height), style.background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH - 1, TITLE - 1), fill=_TITLE_BG)
    if title:
        draw.text((PAD, (TITLE - FONT_SIZE) // 2), style.title, font=font, fill=_TITLE_FG)
    rects: dict[str, Rect] = {}
    more: dict[str, list[Rect]] = {}
    for kind, value, line, col, cells in placed:
        x = PAD + col * char
        y = TITLE + PAD + line * LINE
        if kind == "text":
            draw.text((x, y + (LINE - FONT_SIZE) / 2 - 1), value, font=font, fill=style.foreground)
            continue
        box = (x, y + 2, x + cells * char, y + LINE - 2)
        draw.rectangle(box, fill=BOX_FILL, outline=BOX_BORDER)
        rect = Rect(x=round(box[0], 2), y=box[1], w=round(box[2] - box[0], 2), h=box[3] - box[1])
        if value in rects:
            more.setdefault(value, []).append(rect)
        else:
            rects[value] = rect

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return RenderedWindow(png=buffer.getvalue(), width=WIDTH, height=height, rects=rects, char_width=char,
                          more={key: tuple(value) for key, value in more.items()})
