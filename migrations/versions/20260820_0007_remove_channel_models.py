"""Move LLM model selection completely to the user frontend.

Revision ID: 20260820_0007
Revises: 20260820_0006
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0007"
down_revision: str | None = "20260820_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("model_channels", "model_names")
    op.drop_column("model_channels", "model_name")


def downgrade() -> None:
    op.add_column(
        "model_channels",
        sa.Column("model_name", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "model_channels",
        sa.Column("model_names", sa.JSON(), nullable=True),
    )
    op.execute("UPDATE model_channels SET model_name = 'gpt-4o-mini'")
    op.execute("UPDATE model_channels SET model_names = json_build_array(model_name)")
    op.alter_column("model_channels", "model_name", nullable=False)
    op.alter_column("model_channels", "model_names", nullable=False)
