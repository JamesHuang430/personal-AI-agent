"""Persist selectable video resolution for director projects and jobs.

Revision ID: 20260827_0014
Revises: 20260827_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0014"
down_revision: str | Sequence[str] | None = "20260827_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "director_projects",
        sa.Column(
            "resolution",
            sa.String(length=16),
            server_default=sa.text("'768P'"),
            nullable=False,
        ),
    )
    op.add_column(
        "video_jobs",
        sa.Column(
            "resolution",
            sa.String(length=16),
            server_default=sa.text("'768P'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("video_jobs", "resolution")
    op.drop_column("director_projects", "resolution")
