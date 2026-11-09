"""Feste Liste: a session may run as a fixed task list, items carry their list place.

`learning_adaptive_sessions.ablauf` tells the adaptive flow ("anpassend", the default of
every existing row) from the fixed list ("liste": all attempts exist from the start).
`learning_adaptive_items.listen_platz` is the author's order from the Werkstatt release.
No backfill: the items table is append-only, old items keep a NULL place and fall back to
`activity_id` order.

Plain ALTERs only: on SQLite a batch rebuild would drop the unnamed CHECKs of both tables, so
the new CHECKs are created on PostgreSQL only (the models carry them for create_all).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_feste_liste"
down_revision = "0020_vorbewertungen"
branch_labels = None
depends_on = None


def _postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "learning_adaptive_sessions",
        sa.Column("ablauf", sa.String(16), nullable=False, server_default="anpassend"),
    )
    op.add_column(
        "learning_adaptive_items", sa.Column("listen_platz", sa.Integer(), nullable=True)
    )
    if _postgres():
        op.create_check_constraint(
            "ck_learning_adaptive_sessions_ablauf",
            "learning_adaptive_sessions",
            "ablauf IN ('anpassend','liste')",
        )
        op.create_check_constraint(
            "ck_learning_adaptive_items_listen_platz",
            "learning_adaptive_items",
            "listen_platz IS NULL OR listen_platz >= 1",
        )


def downgrade() -> None:
    if _postgres():
        op.drop_constraint(
            "ck_learning_adaptive_items_listen_platz", "learning_adaptive_items", type_="check"
        )
        op.drop_constraint(
            "ck_learning_adaptive_sessions_ablauf", "learning_adaptive_sessions", type_="check"
        )
    op.drop_column("learning_adaptive_items", "listen_platz")
    op.drop_column("learning_adaptive_sessions", "ablauf")
