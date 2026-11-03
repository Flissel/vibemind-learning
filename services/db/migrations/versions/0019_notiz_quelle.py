"""Notizen: Quelle je Notiz (Sequenzplayer oder Bewertung einer Antwort)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_notiz_quelle"
down_revision = "0018_kurs_bewertungen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("learning_notes") as batch:
        batch.add_column(
            sa.Column("quelle", sa.String(16), nullable=False, server_default="sequenz")
        )


def downgrade() -> None:
    with op.batch_alter_table("learning_notes") as batch:
        batch.drop_column("quelle")