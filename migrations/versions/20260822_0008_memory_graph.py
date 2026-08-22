"""Add persistent conversations, vector memories, and the Apache AGE graph.

Revision ID: 20260822_0008
Revises: 20260820_0007
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20260822_0008"
down_revision: str | None = "20260820_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS age")
    op.execute("LOAD 'age'")
    op.execute('SET search_path = ag_catalog, "$user", public')
    op.execute(
        """
        DO $block$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'user_memory'
            ) THEN
                PERFORM ag_catalog.create_graph('user_memory');
            END IF;
        END
        $block$;
        """
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_user_recent",
        "conversations",
        ["user_id", "last_message_at"],
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("channel_name", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_message_role"),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "conversation_messages",
        ["conversation_id", "created_at"],
    )
    op.create_index("ix_conversation_messages_user_id", "conversation_messages", ["user_id"])

    op.create_table(
        "memory_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("memory_type", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), server_default=sa.text("0.7"), nullable=False),
        sa.Column("importance", sa.Float(), server_default=sa.text("0.5"), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"], ["conversation_messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "content_hash", name="uq_memory_user_content_hash"),
        sa.CheckConstraint(
            "memory_type IN ('preference', 'fact', 'goal', 'event', 'constraint')",
            name="ck_memory_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'deleted')", name="ck_memory_status"
        ),
    )
    op.create_index(
        "ix_memory_items_user_active",
        "memory_items",
        ["user_id", "status", "memory_type"],
    )

    op.create_table(
        "memory_embeddings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["memory_id"], ["memory_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_id", "embedding_model", name="uq_memory_embedding_model"),
    )
    op.create_index(
        "ix_memory_embeddings_user_model",
        "memory_embeddings",
        ["user_id", "embedding_model", "dimensions"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_embeddings_user_model", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")
    op.drop_index("ix_memory_items_user_active", table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_index("ix_conversation_messages_user_id", table_name="conversation_messages")
    op.drop_index("ix_messages_conversation_created", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_user_recent", table_name="conversations")
    op.drop_table("conversations")
    # AGE and the graph are intentionally retained to avoid destructive data loss.
