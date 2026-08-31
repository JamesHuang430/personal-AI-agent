"""Record HTTP/model requests and allow director confirmation state.

Revision ID: 20260831_0016
Revises: 20260828_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0016"
down_revision: str | Sequence[str] | None = "20260828_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_director_project_status", "director_projects", type_="check"
    )
    op.create_check_constraint(
        "ck_director_project_status",
        "director_projects",
        "status IN ('awaiting_confirmation', 'queued', 'processing', 'completed', 'failed')",
    )

    op.create_table(
        "request_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("actor", sa.String(length=320)),
        sa.Column("method", sa.String(length=16)),
        sa.Column("path", sa.String(length=500)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("model_name", sa.String(length=200)),
        sa.Column("input_payload", sa.Text()),
        sa.Column("output_payload", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "request_id",
        "category",
        "actor",
        "path",
        "status_code",
        "model_name",
        "created_at",
    ):
        op.create_index(f"ix_request_logs_{column}", "request_logs", [column])


def downgrade() -> None:
    op.drop_table("request_logs")
    op.drop_constraint(
        "ck_director_project_status", "director_projects", type_="check"
    )
    op.create_check_constraint(
        "ck_director_project_status",
        "director_projects",
        "status IN ('queued', 'processing', 'completed', 'failed')",
    )
