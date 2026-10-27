"""Kurs-Werkstatt: courses, tasks, numbered versions (Fassungen) and jobs."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_werkstatt"
down_revision = "0013_source_watch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_werkstatt_kurse",
        sa.Column("course_id", sa.String(128), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("audience", sa.String(500), nullable=False),
        sa.Column("default_kind", sa.String(40), nullable=False),
        sa.Column("outcomes", sa.JSON(), nullable=False),
        sa.Column("source_provenance", sa.JSON(), nullable=False),
        sa.Column("owner_ref", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "learning_werkstatt_aufgaben",
        sa.Column("task_id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(128), nullable=False),
        sa.Column("chapter_title", sa.String(300), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_learning_werkstatt_aufgaben_course_id", "learning_werkstatt_aufgaben", ["course_id"]
    )
    op.create_table(
        "learning_werkstatt_fassungen",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("based_on", sa.Integer(), nullable=True),
        sa.Column("author_ref", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("art", sa.String(20), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "number", name="learning_werkstatt_fassungen_task_number_key"),
        sa.CheckConstraint("art IN ('neu','textkorrektur','zurueckgeholt','uebernommen')"),
    )
    op.create_index(
        "ix_learning_werkstatt_fassungen_task_id", "learning_werkstatt_fassungen", ["task_id"]
    )
    op.create_table(
        "learning_werkstatt_auftraege",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("course_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('grobentwurf','ausformulieren')"),
        sa.CheckConstraint("state IN ('queued','running','fertig','fehler')"),
    )
    op.create_index(
        "ix_learning_werkstatt_auftraege_course_id", "learning_werkstatt_auftraege", ["course_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_learning_werkstatt_auftraege_course_id", table_name="learning_werkstatt_auftraege"
    )
    op.drop_table("learning_werkstatt_auftraege")
    op.drop_index(
        "ix_learning_werkstatt_fassungen_task_id", table_name="learning_werkstatt_fassungen"
    )
    op.drop_table("learning_werkstatt_fassungen")
    op.drop_index(
        "ix_learning_werkstatt_aufgaben_course_id", table_name="learning_werkstatt_aufgaben"
    )
    op.drop_table("learning_werkstatt_aufgaben")
    op.drop_table("learning_werkstatt_kurse")
