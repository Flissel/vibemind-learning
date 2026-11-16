"""Test cases of a programming task: comparison, fingerprint and the run.

Pure: no WSL, no model. A test case feeds text to the program's stdin and expects text on
stdout. The executor is passed in (`huelle.laeufer.ausfuehren` on the PC), so the VM services
can import this module for the schema and the fingerprint without being able to run code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TESTS = 10
# What a run may do beyond the base level (compute, print); the code runner enforces it, the network stays closed.
Erlaubnis = Literal["pakete", "diagramm", "lange_laufzeit", "daten"]
ERLAUBNISSE: tuple[str, ...] = ("pakete", "diagramm", "lange_laufzeit", "daten")
MAX_DATEIEN = 5
MAX_DATEIEN_ZEICHEN = 200_000


class Testfall(BaseModel):
    __test__: ClassVar[bool] = False  # not a pytest class
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    eingabe: str = Field("", max_length=4_000)
    erwartet: str = Field(max_length=4_000)


class Datei(BaseModel):
    """A small text file of a task (exercise data); the run sees it read-only under `daten/`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,59}$")
    inhalt: str = Field(max_length=100_000)


class TestErgebnis(BaseModel):
    """How a program did on the test cases; `fehlgeschlagen` names every failed one."""

    __test__: ClassVar[bool] = False
    model_config = ConfigDict(extra="forbid", frozen=True)

    bestanden: int = Field(ge=0, le=MAX_TESTS)
    gesamt: int = Field(ge=0, le=MAX_TESTS)
    fehlgeschlagen: tuple[str, ...] = Field(default=(), max_length=MAX_TESTS)

    @model_validator(mode="after")
    def _stimmig(self) -> Self:
        if self.bestanden + len(self.fehlgeschlagen) != self.gesamt:
            raise ValueError("bestanden und fehlgeschlagen ergeben nicht gesamt")
        return self

    @property
    def alle_bestanden(self) -> bool:
        return self.gesamt > 0 and self.bestanden == self.gesamt


def _normal(text: str) -> str:
    zeilen = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(zeile.rstrip() for zeile in zeilen).rstrip()


def gleich(ausgabe: str, erwartet: str) -> bool:
    """Same output: line endings unified, whitespace at line ends and at the text end ignored."""
    return _normal(ausgabe) == _normal(erwartet)


def fingerabdruck(
    sprache: str, musterloesung: str, tests: Sequence[Testfall], *,
    erlaubnisse: Sequence[str] = (), dateien: Sequence[Datei] = (),
) -> str:
    """What a test run of the reference solution vouches for; the order of tests, permissions and
    files is free. Permissions and files only enter when there are any, so tasks without them keep
    the fingerprint they had before permissions existed."""
    faelle = sorted((t.name, t.eingabe, t.erwartet) for t in tests)
    teile: list[object] = [sprache, musterloesung, faelle]
    if erlaubnisse or dateien:
        teile += [sorted(erlaubnisse), sorted((d.name, d.inhalt) for d in dateien)]
    roh = json.dumps(teile, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()


def tests_laufen(
    sprache: str, quelltext: str, tests: Sequence[Testfall], *, ausfuehren: Callable[[str, str, str], Any]
) -> TestErgebnis:
    """Run `quelltext` once per test case. A crash (exit code not 0) or a timeout fails the case.

    `ausfuehren(sprache, quelltext, eingabe)` returns an object with `exit_code`, `stdout`
    and `zeit_ueberschritten`; its errors are not caught here.
    """
    fehlgeschlagen = []
    for test in tests:
        lauf = ausfuehren(sprache, quelltext, test.eingabe)
        ok = lauf.exit_code == 0 and not lauf.zeit_ueberschritten and gleich(lauf.stdout, test.erwartet)
        if not ok:
            fehlgeschlagen.append(test.name)
    return TestErgebnis(
        bestanden=len(tests) - len(fehlgeschlagen), gesamt=len(tests), fehlgeschlagen=tuple(fehlgeschlagen)
    )


tests_laufen.__test__ = False  # type: ignore[attr-defined]  # the name starts with "test": not a pytest test
