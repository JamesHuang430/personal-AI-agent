"""Independent image channels and resumable whiteboard production."""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0020"
down_revision = "20260905_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "image_channels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("qps_limit", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "director_projects",
        sa.Column("production_mode", sa.String(20), nullable=False, server_default="video"),
    )
    op.add_column("director_shots", sa.Column("image_path", sa.String(500)))
    op.add_column("director_shots", sa.Column("image_source", sa.String(20)))
    op.add_column(
        "director_shots",
        sa.Column(
            "image_channel_id", sa.Uuid(), sa.ForeignKey("image_channels.id", ondelete="RESTRICT")
        ),
    )
    op.add_column(
        "director_shots", sa.Column("image_submission_started_at", sa.DateTime(timezone=True))
    )


def downgrade():
    for name in ("image_submission_started_at", "image_channel_id", "image_source", "image_path"):
        op.drop_column("director_shots", name)
    op.drop_column("director_projects", "production_mode")
    op.drop_table("image_channels")
