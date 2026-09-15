"""Director sound settings and provider subtitle timestamps; keyless Edge speech."""

import sqlalchemy as sa
from alembic import op

revision = "20260915_0021"
down_revision = "20260909_0020"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "director_projects",
        sa.Column(
            "postproduction", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")
        ),
    )
    op.add_column(
        "speech_jobs",
        sa.Column("timing", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.alter_column("speech_jobs", "channel_id", existing_type=sa.Uuid(), nullable=True)


def downgrade():
    # Do not delete keyless jobs or arbitrarily assign them to a paid channel.
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM speech_jobs WHERE channel_id IS NULL)"))
        .scalar()
    ):
        raise RuntimeError("存在无渠道 Edge 配音任务，不能无损回退；请保留本迁移")
    op.alter_column("speech_jobs", "channel_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("speech_jobs", "timing")
    op.drop_column("director_projects", "postproduction")
