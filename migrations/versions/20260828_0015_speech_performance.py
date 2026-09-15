"""Persist stable role voices and per-scene speech emotion.

Revision ID: 20260828_0015
Revises: 20260827_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0015"
down_revision: str | Sequence[str] | None = "20260827_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("speech_jobs", sa.Column("speaker", sa.String(length=100)))
    op.add_column("speech_jobs", sa.Column("voice_role", sa.String(length=32)))
    op.add_column(
        "speech_jobs",
        sa.Column(
            "emotion",
            sa.String(length=32),
            server_default=sa.text("'calm'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("speech_jobs", "emotion")
    op.drop_column("speech_jobs", "voice_role")
    op.drop_column("speech_jobs", "speaker")
