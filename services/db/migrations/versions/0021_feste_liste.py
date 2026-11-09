"""Feste Liste: a session may run as a fixed task list, items carry their list place.

`learning_adaptive_sessions.ablauf` tells the adaptive flow ("anpassend", the default of
every existing row) from the fixed list ("liste": all attempts exist from the start).
`learning_adaptive_items.listen_platz` is the author's order from the Werkstatt release.
No backfill: the items table is append-only, old items keep a NULL place and fall back to
`activity_id` order.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_feste_liste"
down_revision = "0020_vorbewertungen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("learning_adaptive_sessions") as batch:
        batch.add_column(
            sa.Column(
                "ablauf", sa.String(16), nullable=False, server_default="anpassend"
            )
        )
        batch.create_check_constraint(
            "ck_learning_adaptive_sessions_ablauf", "ablauf IN ('anpassend','liste')"
        )
    with op.batch_alter_table("learning_adaptive_items") as batch:
        batch.add_column(sa.Column("listen_platz", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_learning_adaptive_items_listen_platz",
            "listen_platz IS NULL OR listen_platz >= 1",
        )


def downgrade() -> None:
    with op.batch_alter_table("learning_adaptive_items") as batch:
        batch.drop_constraint("ck_learning_adaptive_items_listen_platz", type_="check")
        batch.drop_column("listen_platz")
    with op.batch_alter_table("learning_adaptive_sessions") as batch:
        batch.drop_constraint("ck_learning_adaptive_sessions_ablauf", type_="check")
        batch.drop_column("ablauf")
