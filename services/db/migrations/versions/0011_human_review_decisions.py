"""Human review decisions: a person confirms or overrides an AI evaluation.

Pending reviews (low confidence, ungrounded feedback) had no way to be decided. A
decision adds a "manual" evaluation next to the AI one (which stays as evidence),
so evaluations become unique per response *and* evaluator type, and records the
decision in an append-only table.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_human_review_decisions"
down_revision = "0010_learning_migration"
branch_labels = None
depends_on = None


# SQLite created the 0007 unique constraint without a name; this convention gives the
# reflected constraint the PostgreSQL name so both dialects drop the same constraint.
_NAMING = {"uq": "%(table_name)s_%(column_0_name)s_key"}


def upgrade() -> None:
    with op.batch_alter_table(
        "learning_adaptive_evaluations", naming_convention=_NAMING
    ) as batch:
        batch.drop_constraint("learning_adaptive_evaluations_response_id_key", type_="unique")
        batch.create_unique_constraint(
            "learning_adaptive_evaluations_response_evaluator_key",
            ["response_id", "evaluator_type"],
        )
    op.create_table(
        "learning_adaptive_review_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "review_item_id", sa.String(36),
            sa.ForeignKey("learning_adaptive_review_items.id"), nullable=False,
        ),
        sa.Column(
            "ai_evaluation_id", sa.String(36),
            sa.ForeignKey("learning_adaptive_evaluations.id"), nullable=False,
        ),
        sa.Column(
            "human_evaluation_id", sa.String(36),
            sa.ForeignKey("learning_adaptive_evaluations.id"), nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reviewer_id", sa.String(128), nullable=False),
        sa.Column("note", sa.String(2_000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("review_item_id"),
        sa.CheckConstraint("decision IN ('confirm','override')"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION learning_reject_learning_adaptive_review_decisions_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'learning_adaptive_review_decisions is append-only';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER learning_adaptive_review_decisions_append_only
            BEFORE UPDATE OR DELETE ON learning_adaptive_review_decisions
            FOR EACH ROW EXECUTE FUNCTION
            learning_reject_learning_adaptive_review_decisions_mutation()
            """
        )


def downgrade() -> None:
    count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM learning_adaptive_review_decisions")
    ).scalar_one()
    if count:
        raise RuntimeError("cannot downgrade persisted human review decisions")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER learning_adaptive_review_decisions_append_only "
            "ON learning_adaptive_review_decisions"
        )
        op.execute("DROP FUNCTION learning_reject_learning_adaptive_review_decisions_mutation()")
    op.drop_table("learning_adaptive_review_decisions")
    with op.batch_alter_table(
        "learning_adaptive_evaluations", naming_convention=_NAMING
    ) as batch:
        batch.drop_constraint(
            "learning_adaptive_evaluations_response_evaluator_key", type_="unique"
        )
        batch.create_unique_constraint(
            "learning_adaptive_evaluations_response_id_key", ["response_id"]
        )
