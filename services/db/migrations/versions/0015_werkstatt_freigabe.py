"""Werkstatt-Freigabe: Herkunft der Aufgaben und die Tabelle der Freigaben."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_werkstatt_freigabe"
down_revision = "0014_werkstatt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learning_werkstatt_aufgaben", sa.Column("herkunft_key", sa.String(200), nullable=True)
    )
    op.add_column(
        "learning_werkstatt_aufgaben",
        sa.Column("lektion_herkunft_key", sa.String(200), nullable=True),
    )
    op.create_table(
        "learning_werkstatt_freigaben",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(36), nullable=False),
        sa.Column("nummer", sa.Integer(), nullable=False),
        sa.Column("zustand", sa.String(16), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("vergleich", sa.JSON(), nullable=False),
        sa.Column("befunde", sa.JSON(), nullable=False),
        sa.Column("learnhouse_revision_vorher", sa.Integer(), nullable=True),
        sa.Column("learnhouse_revision", sa.Integer(), nullable=True),
        sa.Column("approval_ref", sa.String(128), nullable=True),
        sa.Column("angefordert_von", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freigegeben_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "course_id", "nummer", name="learning_werkstatt_freigaben_course_nummer_key"
        ),
        sa.CheckConstraint("zustand IN ('angefordert','freigegeben','verworfen')"),
    )
    op.create_index(
        "ix_learning_werkstatt_freigaben_course_id", "learning_werkstatt_freigaben", ["course_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_learning_werkstatt_freigaben_course_id", table_name="learning_werkstatt_freigaben"
    )
    op.drop_table("learning_werkstatt_freigaben")
    with op.batch_alter_table("learning_werkstatt_aufgaben") as batch:
        batch.drop_column("lektion_herkunft_key")
        batch.drop_column("herkunft_key")
