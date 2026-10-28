"""Datenbank-Sperre der Kursfabrik: festgehaltene Entwuerfe duerfen abgelehnt werden."""

from __future__ import annotations

from alembic import op


revision = "0016_factory_reject_quality_gate"
down_revision = "0015_werkstatt_freigabe"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
        CREATE OR REPLACE FUNCTION learning_guard_factory_job()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE valid_transition boolean := false;
        BEGIN
            IF TG_OP = 'DELETE'
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.course_id IS DISTINCT FROM OLD.course_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'course factory job identity is immutable';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'course factory revision must advance exactly once';
            END IF;
            IF NEW.current_attempt = OLD.current_attempt + 1 THEN
                valid_transition := OLD.state IN ('failed','cancelled','rejected')
                    AND NEW.state = 'queued' AND NEW.terminal_at IS NULL;
            ELSIF NEW.current_attempt = OLD.current_attempt THEN
                valid_transition :=
                    (OLD.state = 'queued' AND NEW.state = 'ingesting') OR
                    (OLD.state = 'ingesting' AND NEW.state = 'structuring') OR
                    (OLD.state = 'structuring' AND NEW.state = 'authoring') OR
                    (OLD.state = 'authoring' AND NEW.state = 'assessing') OR
                    (OLD.state = 'assessing' AND NEW.state = 'verifying') OR
                    (OLD.state = 'verifying' AND NEW.state = 'quality_gate') OR
                    (OLD.state = 'quality_gate' AND NEW.state = 'review_ready') OR
                    (OLD.state = 'quality_gate' AND NEW.state = 'rejected') OR
                    (OLD.state = 'review_ready' AND NEW.state IN ('published','rejected')) OR
                    (OLD.state IN ('queued','ingesting','structuring','authoring',
                        'assessing','verifying','quality_gate','review_ready')
                        AND NEW.state IN ('failed','cancelled'));
            END IF;
            IF NOT valid_transition THEN
                RAISE EXCEPTION 'invalid course factory transition';
            END IF;
            RETURN NEW;
        END;
        $$
        """

_DOWNGRADE_SQL = """
        CREATE FUNCTION learning_guard_factory_job()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE valid_transition boolean := false;
        BEGIN
            IF TG_OP = 'DELETE'
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.course_id IS DISTINCT FROM OLD.course_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'course factory job identity is immutable';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'course factory revision must advance exactly once';
            END IF;
            IF NEW.current_attempt = OLD.current_attempt + 1 THEN
                valid_transition := OLD.state IN ('failed','cancelled','rejected')
                    AND NEW.state = 'queued' AND NEW.terminal_at IS NULL;
            ELSIF NEW.current_attempt = OLD.current_attempt THEN
                valid_transition :=
                    (OLD.state = 'queued' AND NEW.state = 'ingesting') OR
                    (OLD.state = 'ingesting' AND NEW.state = 'structuring') OR
                    (OLD.state = 'structuring' AND NEW.state = 'authoring') OR
                    (OLD.state = 'authoring' AND NEW.state = 'assessing') OR
                    (OLD.state = 'assessing' AND NEW.state = 'verifying') OR
                    (OLD.state = 'verifying' AND NEW.state = 'quality_gate') OR
                    (OLD.state = 'quality_gate' AND NEW.state = 'review_ready') OR
                    (OLD.state = 'review_ready' AND NEW.state IN ('published','rejected')) OR
                    (OLD.state IN ('queued','ingesting','structuring','authoring',
                        'assessing','verifying','quality_gate','review_ready')
                        AND NEW.state IN ('failed','cancelled'));
            END IF;
            IF NOT valid_transition THEN
                RAISE EXCEPTION 'invalid course factory transition';
            END IF;
            RETURN NEW;
        END;
        $$
        """


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgresql():
        op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    if _is_postgresql():
        op.execute(_DOWNGRADE_SQL.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
