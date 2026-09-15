"""Persist real director-agent outputs and rendered dialogue shots.

Revision ID: 20260827_0013
Revises: 20260823_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0013"
down_revision: str | Sequence[str] | None = "20260823_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "director_projects",
        sa.Column(
            "quality_report",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "director_agent_runs",
        sa.Column(
            "result_data",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.add_column("director_shots", sa.Column("speech_job_id", sa.Uuid(), nullable=True))
    op.add_column("director_shots", sa.Column("speaker", sa.String(length=100), nullable=True))
    op.add_column("director_shots", sa.Column("speech_text", sa.Text(), nullable=True))
    op.add_column("director_shots", sa.Column("subtitle_text", sa.Text(), nullable=True))
    op.add_column(
        "director_shots", sa.Column("rendered_path", sa.String(length=500), nullable=True)
    )
    op.create_foreign_key(
        "fk_director_shots_speech_job",
        "director_shots",
        "speech_jobs",
        ["speech_job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_director_shots_speech_job", "director_shots", type_="foreignkey"
    )
    op.drop_column("director_shots", "rendered_path")
    op.drop_column("director_shots", "subtitle_text")
    op.drop_column("director_shots", "speech_text")
    op.drop_column("director_shots", "speaker")
    op.drop_column("director_shots", "speech_job_id")
    op.drop_column("director_agent_runs", "result_data")
    op.drop_column("director_projects", "quality_report")
