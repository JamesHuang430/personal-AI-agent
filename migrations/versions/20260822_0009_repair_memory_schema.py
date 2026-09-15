"""Move memory tables accidentally created in the AGE catalog to public.

Revision ID: 20260822_0009
Revises: 20260822_0008
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260822_0009"
down_revision: str | None = "20260822_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $repair$
        DECLARE
            table_name text;
        BEGIN
            FOREACH table_name IN ARRAY ARRAY[
                'conversations',
                'conversation_messages',
                'memory_items',
                'memory_embeddings'
            ]
            LOOP
                IF to_regclass(format('public.%I', table_name)) IS NULL
                   AND to_regclass(format('ag_catalog.%I', table_name)) IS NOT NULL
                THEN
                    EXECUTE format(
                        'ALTER TABLE ag_catalog.%I SET SCHEMA public',
                        table_name
                    );
                END IF;

                IF to_regclass(format('public.%I', table_name)) IS NULL THEN
                    RAISE EXCEPTION 'Required table public.% is missing', table_name;
                END IF;
            END LOOP;
        END
        $repair$;
        """
    )
    op.execute('SET search_path = "$user", public')


def downgrade() -> None:
    # This migration repairs an invalid schema placement. Moving application
    # tables back into the extension catalog would reintroduce the outage.
    pass
