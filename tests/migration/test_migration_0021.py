"""Migration 0021: new columns arrive without losing any existing CHECK constraint."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import inspect

from spaces.learning.deployment.migrate import _config
from spaces.learning.services.db.session import create_learning_engine

TABELLEN = ("learning_adaptive_sessions", "learning_adaptive_items")


def _checks(url: str) -> dict[str, set[str]]:
    engine = create_learning_engine(url)
    try:
        inspector = inspect(engine)
        return {
            tabelle: {
                " ".join(str(c["sqltext"]).split())
                for c in inspector.get_check_constraints(tabelle)
            }
            for tabelle in TABELLEN
        }
    finally:
        engine.dispose()


def _spalten(url: str) -> dict[str, set[str]]:
    engine = create_learning_engine(url)
    try:
        inspector = inspect(engine)
        return {
            tabelle: {c["name"] for c in inspector.get_columns(tabelle)} for tabelle in TABELLEN
        }
    finally:
        engine.dispose()


def test_0021_behaelt_alle_bestehenden_checks(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    config = _config(url)
    command.upgrade(config, "0020_vorbewertungen")
    vorher = _checks(url)
    assert all(len(werte) >= 4 for werte in vorher.values())

    command.upgrade(config, "0021_feste_liste")
    spalten = _spalten(url)
    assert "ablauf" in spalten["learning_adaptive_sessions"]
    assert "listen_platz" in spalten["learning_adaptive_items"]
    nachher = _checks(url)
    for tabelle in TABELLEN:
        assert vorher[tabelle] <= nachher[tabelle], tabelle

    command.downgrade(config, "0020_vorbewertungen")
    zurueck = _spalten(url)
    assert "ablauf" not in zurueck["learning_adaptive_sessions"]
    assert "listen_platz" not in zurueck["learning_adaptive_items"]
    assert _checks(url) == vorher
