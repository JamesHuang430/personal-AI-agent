"""Add MiniMax video protocol fields and music generation channels.

Revision ID: 20260822_0010
Revises: 20260822_0009
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0010"
down_revision: str | None = "20260822_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "video_channels",
        sa.Column("provider", sa.String(length=32), server_default="openai", nullable=False),
    )
    op.add_column(
        "video_channels",
        sa.Column(
            "default_resolution",
            sa.String(length=16),
            server_default="768P",
            nullable=False,
        ),
    )
    op.create_table(
        "music_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("qps_limit", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "default_format", sa.String(length=16), server_default="mp3", nullable=False
        ),
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
        "uq_music_channels_one_active",
        "music_channels",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_table(
        "music_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("lyrics", sa.Text(), nullable=True),
        sa.Column("is_instrumental", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("provider_job_id", sa.String(length=255), nullable=True),
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
        sa.ForeignKeyConstraint(["channel_id"], ["music_channels.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_music_jobs_channel_id", "music_jobs", ["channel_id"])
    op.create_index("ix_music_jobs_user_id", "music_jobs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_music_jobs_user_id", table_name="music_jobs")
    op.drop_index("ix_music_jobs_channel_id", table_name="music_jobs")
    op.drop_table("music_jobs")
    op.drop_index("uq_music_channels_one_active", table_name="music_channels")
    op.drop_table("music_channels")
    op.drop_column("video_channels", "default_resolution")
    op.drop_column("video_channels", "provider")
