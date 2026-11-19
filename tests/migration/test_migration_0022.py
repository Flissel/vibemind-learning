"""Migration 0022: jeder bestehende Werkstatt-Kurs bekommt die leere Regelliste."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import inspect, text

from spaces.learning.deployment.migrate import _config
from spaces.learning.services.db.session import create_learning_engine


def test_0022_fuegt_die_regelliste_hinzu_und_nimmt_sie_wieder_weg(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    config = _config(url)
    command.upgrade(config, "0021_feste_liste")
    engine = create_learning_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO learning_werkstatt_kurse (course_id, title, audience, default_kind, outcomes, "
                "source_provenance, owner_ref, created_at, updated_at) VALUES ('c1', 'T', 'A', 'quiz', '[]', '[]', "
                "'o', '2026-11-19 00:00:00', '2026-11-19 00:00:00')"
            ))
    finally:
        engine.dispose()

    command.upgrade(config, "0022_werkstatt_code_regeln")
    engine = create_learning_engine(url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT code_regeln FROM learning_werkstatt_kurse")).scalar_one() == "{}"
    finally:
        engine.dispose()

    command.downgrade(config, "0021_feste_liste")
    engine = create_learning_engine(url)
    try:
        assert "code_regeln" not in {c["name"] for c in inspect(engine).get_columns("learning_werkstatt_kurse")}
    finally:
        engine.dispose()