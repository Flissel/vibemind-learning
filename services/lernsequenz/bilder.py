"""Terminal windows drawn from the lesson's own command line.

No image model: every character comes from the verified lesson text, drawn in a fixed-width
font, so the window shows exactly the command the lesson teaches and the position of every
blank is known to the pixel. A blank is drawn as an empty box; its answer is never drawn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from spaces.learning.contracts.lernsequenz import Rect, Shell

WIDTH = 760
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


@lru_cache(maxsize=1)
def _font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT), FONT_SIZE)


def _units(command: str, blanks: dict[str, str]) -> list[tuple[str, str]]:
    """("text", one character) and ("blank", blank_id) in reading order."""
    found = _PLACEHOLDER.findall(command)
    if set(found) != set(blanks) or len(found) != len(set(found)):
        raise ValueError("placeholders and blanks must match one to one")
    units: list[tuple[str, str]] = []
    position = 0
    for match in _PLACEHOLDER.finditer(command):
        units += [("text", ch) for ch in command[position:match.start()]]
        units.append(("blank", match.group(1)))
        position = match.end()
    units += [("text", ch) for ch in command[position:]]
    return units


def render_terminal(shell: Shell, command: str, *, blanks: dict[str, str] | None = None) -> RenderedWindow:
    """The window of ``shell`` with its prompt and ``command``; ``{{id}}`` placeholders become
    empty boxes as wide as their answer in ``blanks`` (the answer itself is never drawn)."""
    style = _STYLES[shell]
    blanks = blanks or {}
    font = _font()
    char = font.getlength("M")
    columns = int((WIDTH - 2 * PAD) // char)
    units = [("text", ch) for ch in style.prompt] + _units(command, blanks)

    # Lay out: text wraps at any character, a box never splits across lines.
    placed: list[tuple[str, str, int, int, int]] = []  # kind, value, row, column, cells
    row = column = 0
    for kind, value in units:
        cells = max(len(blanks[value]), _MIN_BOX_CELLS) + 1 if kind == "blank" else 1
        cells = min(cells, columns)
        if column + cells > columns:
            row, column = row + 1, 0
        placed.append((kind, value, row, column, cells))
        column += cells
    rows = row + 1
    height = TITLE + 2 * PAD + rows * LINE

    image = Image.new("RGB", (WIDTH, height), style.background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH - 1, TITLE - 1), fill=_TITLE_BG)
    draw.text((PAD, (TITLE - FONT_SIZE) // 2), style.title, font=font, fill=_TITLE_FG)
    rects: dict[str, Rect] = {}
    for kind, value, line, col, cells in placed:
        x = PAD + col * char
        y = TITLE + PAD + line * LINE
        if kind == "text":
            draw.text((x, y + (LINE - FONT_SIZE) / 2 - 1), value, font=font, fill=style.foreground)
            continue
        box = (x, y + 2, x + cells * char, y + LINE - 2)
        draw.rectangle(box, fill=BOX_FILL, outline=BOX_BORDER)
        rects[value] = Rect(x=round(box[0], 2), y=box[1], w=round(box[2] - box[0], 2), h=box[3] - box[1])

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return RenderedWindow(png=buffer.getvalue(), width=WIDTH, height=height, rects=rects, char_width=char)
