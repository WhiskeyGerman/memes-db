"""Создать собственные таблицы Identity без секретов открытым текстом."""

from alembic import op
from sqlalchemy import ARRAY, BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создать аккаунты, verifier, token digest, ограничения и outbox."""
    op.create_table(
        "identity_users",
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("login", String(32), unique=True, nullable=True),
        Column("state", String(16), nullable=False),
        Column("roles", ARRAY(String(16)), nullable=False),
        Column("revision", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("deletion_requested_at", DateTime(timezone=True), nullable=True),
        Column("deleted_at", DateTime(timezone=True), nullable=True),
        Column("delete_media", Boolean, nullable=False),
    )
    op.create_table(
        "identity_credentials",
        Column(
            "user_id",
            UUID(as_uuid=True),
            ForeignKey("identity_users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("verifier", String(512), nullable=False),
        Column("changed_at", DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "identity_sessions",
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column(
            "user_id",
            UUID(as_uuid=True),
            ForeignKey("identity_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("revoked_at", DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_identity_sessions_user", "identity_sessions", ["user_id", "created_at"])
    op.create_table(
        "identity_tokens",
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column(
            "session_id",
            UUID(as_uuid=True),
            ForeignKey("identity_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("kind", String(16), nullable=False),
        Column("digest", String(64), unique=True, nullable=False),
        Column("issued_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("consumed_at", DateTime(timezone=True), nullable=True),
        Column("revoked_at", DateTime(timezone=True), nullable=True),
        Column("replacement_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_identity_tokens_session", "identity_tokens", ["session_id"])
    op.create_table(
        "identity_rate_buckets",
        Column("key", String(64), primary_key=True),
        Column("window_started_at", DateTime(timezone=True), nullable=False),
        Column("attempts", Integer, nullable=False),
    )
    op.create_index(
        "ix_identity_rate_buckets_window_started_at", "identity_rate_buckets", ["window_started_at"]
    )
    op.create_table(
        "identity_outbox",
        Column("event_id", UUID(as_uuid=True), primary_key=True),
        Column("user_id", UUID(as_uuid=True), nullable=False),
        Column("delete_media", Boolean, nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column("next_attempt_at", DateTime(timezone=True), nullable=False),
        Column("attempts", Integer, nullable=False),
        Column("delivered_at", DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_identity_outbox_delivery", "identity_outbox", ["delivered_at", "next_attempt_at"]
    )


def downgrade() -> None:
    """Удалить только таблицы сервиса Identity в обратном порядке."""
    op.drop_table("identity_outbox")
    op.drop_table("identity_rate_buckets")
    op.drop_table("identity_tokens")
    op.drop_table("identity_sessions")
    op.drop_table("identity_credentials")
    op.drop_table("identity_users")
