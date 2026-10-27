"""Terminal windows drawn from the lesson's own command: exact text, known blank positions."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from spaces.learning.services.lernsequenz.bilder import BOX_FILL, render_terminal

_TITLE, _PAD, _LINE = 34, 18, 26


def _image(png: bytes) -> Image.Image:
    return Image.open(BytesIO(png)).convert("RGB")


def test_same_input_gives_the_same_png() -> None:
    a = render_terminal("powershell", "irm https://claude.ai/{{wert}} | iex", blanks={"wert": "install.ps1"})
    b = render_terminal("powershell", "irm https://claude.ai/{{wert}} | iex", blanks={"wert": "install.ps1"})
    assert a.png == b.png and a.rects == b.rects


def test_window_size_follows_the_spec() -> None:
    window = render_terminal("powershell", "claude --version")
    assert (window.width, window.height) == (760, _TITLE + 2 * _PAD + _LINE)
    assert _image(window.png).size == (760, window.height)


def test_blank_rect_sits_inside_the_image_and_fits_the_answer() -> None:
    window = render_terminal("powershell", "irm https://claude.ai/{{wert}} | iex", blanks={"wert": "install.ps1"})
    rect = window.rects["wert"]
    char = window.char_width
    assert rect.x >= _PAD and rect.y >= _TITLE and rect.x + rect.w <= window.width and rect.y + rect.h <= window.height
    assert rect.w >= len("install.ps1") * char


def test_the_solution_is_not_drawn() -> None:
    window = render_terminal("cmd", "curl -fsSL https://claude.ai/{{wert}} -o install.cmd", blanks={"wert": "install.cmd"})
    rect = window.rects["wert"]
    image = _image(window.png)
    inside = {image.getpixel((int(rect.x) + dx, int(rect.y) + dy))
              for dx in range(2, int(rect.w) - 2, 3) for dy in range(2, int(rect.h) - 2, 3)}
    assert inside == {BOX_FILL}


def test_long_commands_wrap_and_keep_the_blank_on_one_line() -> None:
    command = ("[Environment]::SetEnvironmentVariable('PATH', \"$currentPath;$env:USERPROFILE\.local\{{wert}}\", "
               "'User') # " + "x" * 40)
    window = render_terminal("powershell", command, blanks={"wert": "bin"})
    rect = window.rects["wert"]
    assert window.height >= _TITLE + 2 * _PAD + 2 * _LINE
    line_top = _TITLE + _PAD + ((rect.y - _TITLE - _PAD) // _LINE) * _LINE
    assert rect.y >= line_top and rect.y + rect.h <= line_top + _LINE


@pytest.mark.parametrize(("shell", "background"), [
    ("powershell", (0x01, 0x24, 0x56)), ("cmd", (0x0C, 0x0C, 0x0C)), ("unix", (0x1E, 0x1E, 0x1E)),
])
def test_each_shell_has_its_background(shell, background) -> None:
    image = _image(render_terminal(shell, "claude --version").png)
    assert image.getpixel((5, _TITLE + 3)) == background


def test_an_unknown_placeholder_is_refused() -> None:
    with pytest.raises(ValueError):
        render_terminal("cmd", "claude {{wert}}", blanks={})


def test_a_repeated_placeholder_draws_every_box_and_lists_the_others() -> None:
    window = render_terminal("cmd", "curl -fsSL https://claude.ai/{{wert}} -o {{wert}} && {{wert}}", blanks={"wert": "install.cmd"})
    assert len(window.more["wert"]) == 2
    first = window.rects["wert"]
    assert all(r.w == first.w and (r.y, r.x) > (first.y, first.x) for r in window.more["wert"])
