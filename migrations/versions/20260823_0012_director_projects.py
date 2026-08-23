"""Add persistent multi-agent director projects.

Revision ID: 20260823_0012
Revises: 20260823_0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_0012"
down_revision: str | Sequence[str] | None = "20260823_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "director_projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column("target_seconds", sa.Integer(), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=16), nullable=False),
        sa.Column("visual_style", sa.String(length=100), nullable=False),
        sa.Column("continuity_notes", sa.Text(), nullable=True),
        sa.Column(
            "continuity_bible", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
        sa.Column("one_click", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("planned_shots", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_shots", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("current_stage", sa.String(length=32), nullable=True),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.Column("preview_video_job_id", sa.Uuid(), nullable=True),
        sa.Column("final_video_path", sa.String(length=500), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["preview_video_job_id"], ["video_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed')",
            name="ck_director_project_status",
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_director_project_progress"),
    )
    op.create_index(
        "ix_director_projects_user_recent",
        "director_projects",
        ["user_id", "created_at"],
    )

    op.create_table(
        "director_agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_key", sa.String(length=32), nullable=False),
        sa.Column("agent_name", sa.String(length=100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("decision_summary", sa.Text(), nullable=True),
        sa.Column("deliverable", sa.Text(), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["director_projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "agent_key", name="uq_director_project_agent"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_director_agent_status",
        ),
    )
    op.create_index(
        "ix_director_agent_project_sequence",
        "director_agent_runs",
        ["project_id", "sequence"],
    )
    op.create_index("ix_director_agent_runs_user_id", "director_agent_runs", ["user_id"])

    op.create_table(
        "director_shots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("seconds", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("video_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "continuity_snapshot", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False
        ),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["director_projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_job_id"], ["video_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "sequence", name="uq_director_project_shot"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_director_shot_status",
        ),
    )
    op.create_index(
        "ix_director_shot_project_sequence", "director_shots", ["project_id", "sequence"]
    )
    op.create_index("ix_director_shots_user_id", "director_shots", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_director_shots_user_id", table_name="director_shots")
    op.drop_index("ix_director_shot_project_sequence", table_name="director_shots")
    op.drop_table("director_shots")
    op.drop_index("ix_director_agent_runs_user_id", table_name="director_agent_runs")
    op.drop_index("ix_director_agent_project_sequence", table_name="director_agent_runs")
    op.drop_table("director_agent_runs")
    op.drop_index("ix_director_projects_user_recent", table_name="director_projects")
    op.drop_table("director_projects")
