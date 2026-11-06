"""Vorbewertungen: preview gradings of the current attempt, append-only.

"Abgeben" stores a preview here and changes nothing in the learning state; only the
preview the learner confirms becomes a response and an evaluation (with the ids kept
here). Unreadable handwriting is stored without grading fields. Rubric previews keep the
PNG itself: their booked answer refers to this row, so a human review sees the writing.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020_vorbewertungen"
down_revision = "0019_notiz_quelle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_vorbewertungen",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "attempt_id", sa.String(36),
            sa.ForeignKey("learning_adaptive_attempts.id"), nullable=False,
        ),
        sa.Column(
            "session_id", sa.String(36),
            sa.ForeignKey("learning_adaptive_sessions.id"), nullable=False,
        ),
        sa.Column("quelle", sa.String(16), nullable=False),
        sa.Column(
            "korrigiert_aus", sa.String(36), sa.ForeignKey("learning_vorbewertungen.id")
        ),
        sa.Column("bild_sha256", sa.String(64)),
        sa.Column("bild_png", sa.LargeBinary()),
        sa.Column("gelesen", sa.JSON()),
        sa.Column("answer", sa.JSON()),
        sa.Column("lesbar", sa.Boolean(), nullable=False),
        sa.Column("response_id", sa.String(36)),
        sa.Column("evaluation_id", sa.String(36)),
        sa.Column("evaluator_type", sa.String(24)),
        sa.Column("score", sa.Float()),
        sa.Column("confidence", sa.Float()),
        sa.Column("accepted", sa.Boolean()),
        sa.Column("rationale_codes", sa.JSON()),
        sa.Column("evaluator_versions", sa.JSON()),
        sa.Column("source_refs", sa.JSON()),
        sa.Column("criteria", sa.JSON()),
        sa.Column("review_reason", sa.String(64)),
        sa.Column("review_details", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quelle IN ('handschrift','getippt')"),
        sa.CheckConstraint(
            "evaluator_type IS NULL OR evaluator_type IN ('deterministic','rubric')"
        ),
        sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)"),
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
    )
    op.create_index(
        "ix_learning_vorbewertungen_attempt_id", "learning_vorbewertungen", ["attempt_id"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION learning_reject_learning_vorbewertungen_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'learning_vorbewertungen is append-only';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER learning_vorbewertungen_append_only
            BEFORE UPDATE OR DELETE ON learning_vorbewertungen
            FOR EACH ROW EXECUTE FUNCTION
            learning_reject_learning_vorbewertungen_mutation()
            """
        )


def downgrade() -> None:
    count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM learning_vorbewertungen")
    ).scalar_one()
    if count:
        raise RuntimeError("cannot downgrade persisted vorbewertungen")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER learning_vorbewertungen_append_only ON learning_vorbewertungen"
        )
        op.execute("DROP FUNCTION learning_reject_learning_vorbewertungen_mutation()")
    op.drop_index("ix_learning_vorbewertungen_attempt_id", table_name="learning_vorbewertungen")
    op.drop_table("learning_vorbewertungen")
