"""Удаление до Published и очистка объектов после истечения PUT."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_processing_deletion"
down_revision = "0001_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Tombstone не требует уже полученного оригинала."""
    op.create_table(
        "processing_tombstones",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "processing_outbox",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False),
    )
    op.create_unique_constraint(
        "uq_processing_outbox_sequence",
        "processing_outbox",
        ["sequence"],
    )
    op.create_index(
        "ix_processing_outbox_source_sequence",
        "processing_outbox",
        ["source_id", "sequence"],
    )
    op.add_column(
        "processing_artifact_cleanup",
        sa.Column("retained", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "processing_artifact_cleanup",
        sa.Column("final_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    """Откат выполняется только отдельной административной командой."""
    op.drop_column("processing_artifact_cleanup", "final_at")
    op.drop_column("processing_artifact_cleanup", "retained")
    op.drop_index("ix_processing_outbox_source_sequence", table_name="processing_outbox")
    op.drop_constraint("uq_processing_outbox_sequence", "processing_outbox")
    op.drop_column("processing_outbox", "sequence")
    op.drop_table("processing_tombstones")
