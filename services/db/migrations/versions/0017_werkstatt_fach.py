"""Werkstatt: Fach je Kurs (terminal oder mathe)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_werkstatt_fach"
down_revision = "0016_factory_reject_quality_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learning_werkstatt_kurse",
        sa.Column("fach", sa.String(20), nullable=False, server_default="terminal"),
    )


def downgrade() -> None:
    op.drop_column("learning_werkstatt_kurse", "fach")
