"""Allow the storyboard review gate on databases upgraded from legacy releases."""

from alembic import op

revision = "20260905_0019"
down_revision = "20260905_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_director_project_status", "director_projects", type_="check")
    op.create_check_constraint(
        "ck_director_project_status",
        "director_projects",
        "status IN ('awaiting_confirmation', 'awaiting_storyboard', "
        "'queued', 'processing', 'completed', 'failed')",
    )


def downgrade():
    # Preserve the user's approval boundary: never turn pending review into queued media.
    op.execute(
        "UPDATE director_projects SET status = 'awaiting_confirmation', "
        "storyboard_approved = false WHERE status = 'awaiting_storyboard'"
    )
    op.drop_constraint("ck_director_project_status", "director_projects", type_="check")
    op.create_check_constraint(
        "ck_director_project_status",
        "director_projects",
        "status IN ('awaiting_confirmation', 'queued', 'processing', 'completed', 'failed')",
    )
