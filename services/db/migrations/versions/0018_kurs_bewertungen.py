"""Kursbewertungen: Smiley und Stichworte je Person und Kurs."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_kurs_bewertungen"
down_revision = "0017_werkstatt_fach"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_course_ratings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("kurs_titel", sa.String(300), nullable=False),
        sa.Column("smiley", sa.String(16), nullable=False),
        sa.Column("stichworte", sa.JSON(), nullable=False),
        sa.Column("erstellt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aktualisiert", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gemeldet", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("course_id", "actor_id", name="uq_learning_course_ratings_course_actor"),
    )
    op.create_index("ix_learning_course_ratings_gemeldet", "learning_course_ratings", ["gemeldet"])


def downgrade() -> None:
    op.drop_index("ix_learning_course_ratings_gemeldet", table_name="learning_course_ratings")
    op.drop_table("learning_course_ratings")
