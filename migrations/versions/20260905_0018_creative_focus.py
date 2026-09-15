"""Creative preferences, project provenance and storyboard review."""

import sqlalchemy as sa
from alembic import op

revision = "20260905_0018"
down_revision = "20260905_0017"
branch_labels = None
depends_on = None


def upgrade():
    for table, column in (
        ("users", "creative_preferences"),
        ("director_projects", "personalization"),
        ("director_projects", "feedback"),
    ):
        op.add_column(
            table,
            sa.Column(column, sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        )
    # Existing work keeps its execution policy; new projects explicitly opt into review.
    for column in ("review_required", "storyboard_approved"):
        op.add_column(
            "director_projects",
            sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade():
    for column in ("storyboard_approved", "review_required", "feedback", "personalization"):
        op.drop_column("director_projects", column)
    op.drop_column("users", "creative_preferences")
