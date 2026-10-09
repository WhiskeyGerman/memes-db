"""Зафиксированная схема первого релиза координатора."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_processing"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создать собственные durable таблицы без импорта текущих ORM моделей."""
    op.create_table(
        "processing_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "processing_workers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "processing_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("processing_sources.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True)),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("source_id", "kind", "generation"),
    )
    op.create_index(
        "ix_processing_tasks_queue", "processing_tasks", ["state", "ready_at", "created_at"]
    )
    op.create_index("ix_processing_tasks_lease", "processing_tasks", ["expires_at"])
    op.create_index("ix_processing_tasks_worker", "processing_tasks", ["worker_id", "state"])
    op.create_table(
        "processing_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("processing_tasks.id"),
            nullable=False,
        ),
        sa.Column(
            "worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("processing_workers.id"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(32), nullable=False),
    )
    op.create_table(
        "processing_inbox", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)
    )
    op.create_table(
        "processing_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_processing_outbox_ready",
        "processing_outbox",
        ["delivered_at", "ready_at", "created_at"],
    )
    op.create_table(
        "processing_artifact_cleanup",
        sa.Column("key", sa.String(1024), primary_key=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_processing_artifact_cleanup_ready_at", "processing_artifact_cleanup", ["ready_at"]
    )


def downgrade() -> None:
    """Откат требует явного запуска административной команды."""
    op.drop_table("processing_artifact_cleanup")
    op.drop_table("processing_outbox")
    op.drop_table("processing_inbox")
    op.drop_table("processing_attempts")
    op.drop_table("processing_tasks")
    op.drop_table("processing_workers")
    op.drop_table("processing_sources")
