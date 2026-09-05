"""Durable queue, chat execution records, and message artifacts."""

import sqlalchemy as sa
from alembic import op

revision = "20260905_0017"
down_revision = "20260831_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column(
            "artifacts",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    for table in ("video_jobs", "music_jobs", "speech_jobs"):
        op.add_column(table, sa.Column("submission_started_at", sa.DateTime(timezone=True)))
        # Legacy interrupted submissions are ambiguous: never silently charge again.
        op.execute(
            sa.text(
                f"UPDATE {table} SET submission_started_at = updated_at "
                "WHERE status IN ('processing', 'failed')"
            )
        )
    op.create_table(
        "work_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("kind", "resource_id", name="uq_work_resource"),
    )
    op.create_index("ix_work_items_status", "work_items", ["status"])
    op.create_table(
        "chat_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="SET NULL")
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("response", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("error_status", sa.Integer()),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_chat_idempotency"),
    )
    op.create_index(
        "uq_chat_active_user",
        "chat_runs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )
    # Adopt old active top-level jobs. Director children are resumed by their parent.
    for kind, table, exclusion in (
        ("director", "director_projects", ""),
        (
            "video",
            "video_jobs",
            "AND NOT EXISTS (SELECT 1 FROM director_shots s WHERE s.video_job_id = j.id)",
        ),
        (
            "speech",
            "speech_jobs",
            "AND NOT EXISTS (SELECT 1 FROM director_shots s WHERE s.speech_job_id = j.id)",
        ),
        ("music", "music_jobs", ""),
    ):
        op.execute(
            sa.text(
                "INSERT INTO work_items (id, kind, resource_id, payload, status, attempts) "
                f"SELECT gen_random_uuid(), '{kind}', j.id, '{{}}'::json, 'queued', 0 "
                f"FROM {table} j "
                f"WHERE j.status IN ('queued', 'processing') {exclusion}"
            )
        )


def downgrade() -> None:
    op.drop_table("chat_runs")
    op.drop_table("work_items")
    op.execute("UPDATE video_jobs SET status='failed' WHERE status='awaiting_confirmation'")
    for table in ("video_jobs", "music_jobs", "speech_jobs"):
        op.drop_column(table, "submission_started_at")
    op.drop_column("conversation_messages", "artifacts")
