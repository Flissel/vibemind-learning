"""What a pasted terminal output tells before any rule runs.

Three things people paste that no output rule should have to know about: a command that
was copied out of Word, a PDF or a chat and arrived with a dash or curly quotes the shell
cannot read; the command itself instead of what it printed; and anything that looks like
a key, which must never be echoed or stored.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_DASH_BEFORE_WORD = re.compile(r"[–—]\s*\w")
_CURLY_QUOTES = re.compile(r"[‘’“”„‚]")
_SECRETS = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._-]{20,}"
)


@dataclass(frozen=True)
class OutputFindings:
    typographic: tuple[str, ...]   # "gedankenstrich", "anfuehrungszeichen"
    looks_like_secret: bool
    is_the_command: bool


def inspect_output(output: str, commands: Iterable[str]) -> OutputFindings:
    typographic: list[str] = []
    if _DASH_BEFORE_WORD.search(output):
        typographic.append("gedankenstrich")
    if _CURLY_QUOTES.search(output):
        typographic.append("anfuehrungszeichen")
    pasted = " ".join(output.split())
    is_the_command = any(pasted == " ".join(command.split()) for command in commands)
    return OutputFindings(tuple(typographic), _SECRETS.search(output) is not None, is_the_command)
