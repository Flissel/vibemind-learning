"""Claw tests: the third-party `mcp` package must win over spaces/learning/mcp.

pytest's default (prepend) import mode puts the first directory without an
`__init__.py` above a test package on sys.path. For deployment/test_compose.py that is
spaces/learning itself, which holds a local `mcp` package; any lazy `import mcp...` in the
sidecar then resolves to the wrong one. This fixture hides that directory for the claw tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

LEARNING_ROOT = os.path.normcase(str(Path(__file__).resolve().parents[2]))


def _inside_learning(module: object) -> bool:
    origin = getattr(module, "__file__", None) or ""
    return os.path.normcase(os.path.abspath(origin)).startswith(LEARNING_ROOT + os.sep)


@pytest.fixture(autouse=True)
def _real_mcp_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "path", [p for p in sys.path if os.path.normcase(os.path.abspath(p or ".")) != LEARNING_ROOT]
    )
    for name in [n for n in sys.modules if n == "mcp" or n.startswith("mcp.")]:
        if _inside_learning(sys.modules[name]):
            monkeypatch.delitem(sys.modules, name)
