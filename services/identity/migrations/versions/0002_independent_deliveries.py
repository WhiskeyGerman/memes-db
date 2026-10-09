"""Добавить independent REST deliveries с lease fencing без изменения старых событий."""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_independent_deliveries"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создать отдельные назначения; старый undelivered outbox раскрывается адаптером."""
    op.create_index(
        "ix_identity_deleted_owners",
        "identity_users",
        ["id"],
        postgresql_where=text("state = 'deleted'"),
    )
    op.create_table(
        "identity_outbox_deliveries",
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column(
            "event_id",
            UUID(as_uuid=True),
            ForeignKey("identity_outbox.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("recipient", String(32), nullable=False),
        Column("base_url", String(2048), nullable=False),
        Column("next_attempt_at", DateTime(timezone=True), nullable=False),
        Column("attempts", Integer, nullable=False),
        Column("delivered_at", DateTime(timezone=True), nullable=True),
        Column("claim_token", UUID(as_uuid=True), nullable=True),
        Column("lease_until", DateTime(timezone=True), nullable=True),
        UniqueConstraint("event_id", "recipient", "base_url", name="uq_identity_delivery_target"),
    )
    op.create_index(
        "ix_identity_delivery_claim",
        "identity_outbox_deliveries",
        ["delivered_at", "next_attempt_at", "lease_until"],
    )


def downgrade() -> None:
    """Удалить новые назначения, сохранив исходный outbox для повторной доставки."""
    op.drop_table("identity_outbox_deliveries")
    op.drop_index("ix_identity_deleted_owners", table_name="identity_users")
