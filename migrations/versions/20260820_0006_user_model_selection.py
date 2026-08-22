"""Allow users to select models from the active LLM channel.

Revision ID: 20260820_0006
Revises: 20260820_0005
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0006"
down_revision: str | None = "20260820_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_channels",
        sa.Column("model_names", sa.JSON(), nullable=True),
    )
    op.execute("UPDATE model_channels SET model_names = json_build_array(model_name)")
    op.alter_column("model_channels", "model_names", nullable=False)


def downgrade() -> None:
    op.drop_column("model_channels", "model_names")
