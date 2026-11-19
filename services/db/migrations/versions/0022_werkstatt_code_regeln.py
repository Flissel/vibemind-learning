"""Werkstatt: Regelliste je Kurs fuer die Erlaubnisse der Programmieraufgaben.

`learning_werkstatt_kurse.code_regeln` holds, per permission, "ohne_frage" or "nie"; a missing
key means "mit_frage". Every existing course starts with the empty list, which is exactly the
behaviour before this migration.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0022_werkstatt_code_regeln"
down_revision = "0021_feste_liste"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learning_werkstatt_kurse",
        sa.Column("code_regeln", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("learning_werkstatt_kurse", "code_regeln")
