"""Watched source pages per course: the digest of the visible text the course was built
from, when it was last checked, and when it was found changed (nightly run)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_source_watch"
down_revision = "0012_learning_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_source_watch",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(128), nullable=False),
        sa.Column("url", sa.String(2_000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("course_id", "url", name="learning_source_watch_course_url_key"),
    )
    op.create_index("ix_learning_source_watch_course_id", "learning_source_watch", ["course_id"])


def downgrade() -> None:
    op.drop_index("ix_learning_source_watch_course_id", table_name="learning_source_watch")
    op.drop_table("learning_source_watch")
