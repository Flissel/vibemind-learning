"""Notes a person takes in the sequence player ("Stelle markieren" -> "Notiz anlegen").

Each note keeps what was marked, the course's own explanation of it (grounded), an
optional addition the tutor made from outside the course (always kept apart, with its
sources), and the person's own text. Notes belong to one learner id only.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_learning_notes"
down_revision = "0011_human_review_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_notes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("course_id", sa.String(128), nullable=False),
        sa.Column("sequence_id", sa.String(128), nullable=False),
        sa.Column("anchor", sa.JSON(), nullable=False),
        sa.Column("selection", sa.String(2_000), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("extra", sa.Text(), nullable=False),
        sa.Column("extra_sources", sa.JSON(), nullable=False),
        sa.Column("own_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_learning_notes_actor_id", "learning_notes", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_learning_notes_actor_id", table_name="learning_notes")
    op.drop_table("learning_notes")
