"""Add generated files, video channels, and video jobs.

Revision ID: 20260820_0004
Revises: 20260820_0003
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0004"
down_revision: str | None = "20260820_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "video_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("qps_limit", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "default_seconds", sa.String(length=8), server_default=sa.text("'4'"), nullable=False
        ),
        sa.Column(
            "default_size",
            sa.String(length=20),
            server_default=sa.text("'1280x720'"),
            nullable=False,
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
        "uq_video_channels_one_active",
        "video_channels",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_table(
        "generated_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generated_files_user_id", "generated_files", ["user_id"])
    op.create_table(
        "video_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'queued'"), nullable=False
        ),
        sa.Column("provider_job_id", sa.String(length=255), nullable=True),
        sa.Column("seconds", sa.String(length=8), nullable=False),
        sa.Column("size", sa.String(length=20), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["channel_id"], ["video_channels.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_video_jobs_channel_id", "video_jobs", ["channel_id"])
    op.create_index("ix_video_jobs_user_id", "video_jobs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_video_jobs_user_id", table_name="video_jobs")
    op.drop_index("ix_video_jobs_channel_id", table_name="video_jobs")
    op.drop_table("video_jobs")
    op.drop_index("ix_generated_files_user_id", table_name="generated_files")
    op.drop_table("generated_files")
    op.drop_index("uq_video_channels_one_active", table_name="video_channels")
    op.drop_table("video_channels")
