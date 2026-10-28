"""Migration 0016: die Datenbank-Sperre erlaubt quality_gate -> rejected."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from unittest import mock

VERSIONS = Path(__file__).resolve().parents[2] / "services" / "db" / "migrations" / "versions"
NEW_CLAUSE = "(OLD.state = 'quality_gate' AND NEW.state = 'rejected')"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _body_0004() -> str:
    source = (VERSIONS / "0004_course_factory.py").read_text(encoding="utf-8")
    match = re.search(
        r"CREATE FUNCTION learning_guard_factory_job\(\)(.*?)\$\$\n\s*\"\"\"", source, re.S
    )
    assert match is not None
    return match.group(1)


def _executed(module: ModuleType, dialect: str, fn: str) -> list[str]:
    bind = mock.Mock()
    bind.dialect.name = dialect
    with mock.patch.object(module.op, "get_bind", return_value=bind), mock.patch.object(
        module.op, "execute"
    ) as execute:
        getattr(module, fn)()
    return [str(call.args[0]) for call in execute.call_args_list]


def test_upgrade_keeps_every_0004_clause_and_adds_reject_from_quality_gate() -> None:
    module = _load("0016_factory_reject_quality_gate")
    assert module.down_revision == "0015_werkstatt_freigabe"
    (sql,) = _executed(module, "postgresql", "upgrade")
    assert "CREATE OR REPLACE FUNCTION learning_guard_factory_job()" in sql
    assert NEW_CLAUSE in sql
    for line in _body_0004().splitlines():
        if line.strip():
            assert line.strip() in sql, line


def test_downgrade_restores_0004_body_without_new_clause() -> None:
    module = _load("0016_factory_reject_quality_gate")
    (sql,) = _executed(module, "postgresql", "downgrade")
    assert NEW_CLAUSE not in sql
    for line in _body_0004().splitlines():
        if line.strip():
            assert line.strip() in sql, line


def test_migration_is_noop_on_sqlite() -> None:
    module = _load("0016_factory_reject_quality_gate")
    assert _executed(module, "sqlite", "upgrade") == []
    assert _executed(module, "sqlite", "downgrade") == []
