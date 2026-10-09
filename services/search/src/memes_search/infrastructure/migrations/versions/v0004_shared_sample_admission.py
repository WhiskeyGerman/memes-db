"""Создать общий primary ledger без private payload; backfill pending до rollout."""

from alembic import op
from sqlalchemy import CheckConstraint, Column, DateTime
from sqlalchemy.dialects.postgresql import UUID

revision = "search_0004"
down_revision = "search_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создать reservations и перенести живые primary pending samples."""
    op.create_table(
        "search_sample_admissions",
        Column("sample_id", UUID(as_uuid=True), primary_key=True),
        Column("owner_id", UUID(as_uuid=True), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("expires_at > created_at", name="ck_search_sample_admission_lifetime"),
    )
    op.create_index(
        "ix_search_sample_admissions_owner", "search_sample_admissions", ["owner_id", "expires_at"]
    )
    op.create_index(
        "ix_search_sample_admissions_expiry", "search_sample_admissions", ["expires_at"]
    )
    op.execute("""
        INSERT INTO search_sample_admissions (sample_id, owner_id, created_at, expires_at)
        SELECT id, owner_id, (body->>'created_at')::timestamptz, expires_at
        FROM search_samples
        WHERE state IN ('uploading', 'queued') AND expires_at > now()
        AND NOT EXISTS (
            SELECT 1 FROM search_deleted_owners d WHERE d.owner_id=search_samples.owner_id
        )
    """)

    op.execute("""
        UPDATE search_outbox
        SET sample_id=NULL, body=jsonb_build_object('sample_id', body->>'id')
        WHERE action='cancel' AND body ? 'id'
    """)


def downgrade() -> None:
    """Удалить только admission ledger, сохранив private samples."""
    op.drop_table("search_sample_admissions")
