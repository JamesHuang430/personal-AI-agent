"""Add MiniMax speech channels and generated speech jobs.

Revision ID: 20260823_0011
Revises: 20260822_0010
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_0011"
down_revision: str | None = "20260822_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "speech_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("default_voice_id", sa.String(length=200), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("qps_limit", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("default_format", sa.String(length=16), server_default="mp3", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_speech_channels_one_active",
        "speech_channels",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_table(
        "speech_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("speech_text", sa.Text(), nullable=False),
        sa.Column("voice_id", sa.String(length=200), nullable=False),
        sa.Column("speed", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("audio_format", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("storage_path", sa.String(length=500), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["channel_id"], ["speech_channels.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_speech_jobs_channel_id", "speech_jobs", ["channel_id"])
    op.create_index("ix_speech_jobs_user_id", "speech_jobs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_speech_jobs_user_id", table_name="speech_jobs")
    op.drop_index("ix_speech_jobs_channel_id", table_name="speech_jobs")
    op.drop_table("speech_jobs")
    op.drop_index("uq_speech_channels_one_active", table_name="speech_channels")
    op.drop_table("speech_channels")
