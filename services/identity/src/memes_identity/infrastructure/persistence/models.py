"""ORM-записи Identity; бизнес-модель не зависит от SQLAlchemy."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from memes_identity.domain.user import UserState


class TableName(StrEnum):
    """Имена таблиц одного владельца данных."""

    USERS = "identity_users"
    CREDENTIALS = "identity_credentials"
    SESSIONS = "identity_sessions"
    TOKENS = "identity_tokens"
    RATE_BUCKETS = "identity_rate_buckets"
    OUTBOX = "identity_outbox"
    DELIVERIES = "identity_outbox_deliveries"


class IdentityBase(DeclarativeBase):
    """Метаданные таблиц только сервиса Identity."""


class UserRow(IdentityBase):
    """Бизнес-состояние пользователя и обезличенный tombstone."""

    __tablename__ = TableName.USERS

    id: Mapped[UUID] = mapped_column(primary_key=True)
    login: Mapped[str | None] = mapped_column(String(32), unique=True)
    state: Mapped[str] = mapped_column(String(16))
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_media: Mapped[bool] = mapped_column(Boolean, default=False)


class CredentialRow(IdentityBase):
    """Argon2id verifier, очищаемый при удалении аккаунта."""

    __tablename__ = TableName.CREDENTIALS

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{TableName.USERS}.id", ondelete="CASCADE"), primary_key=True
    )
    verifier: Mapped[str] = mapped_column(String(512))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionRow(IdentityBase):
    """Семейство opaque-токенов с абсолютным сроком жизни."""

    __tablename__ = TableName.SESSIONS
    __table_args__ = (Index("ix_identity_sessions_user", "user_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey(f"{TableName.USERS}.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TokenRow(IdentityBase):
    """SHA-256 digest токена; исходный токен в БД не попадает."""

    __tablename__ = TableName.TOKENS
    __table_args__ = (Index("ix_identity_tokens_session", "session_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{TableName.SESSIONS}.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replacement_id: Mapped[UUID | None] = mapped_column()


class RateBucketRow(IdentityBase):
    """Короткоживущий счётчик, не содержащий логин либо IP открытым текстом."""

    __tablename__ = TableName.RATE_BUCKETS

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer)


class AccountOutboxRow(IdentityBase):
    """Надёжное идемпотентное уведомление о политике удаления."""

    __tablename__ = TableName.OUTBOX
    __table_args__ = (Index("ix_identity_outbox_delivery", "delivered_at", "next_attempt_at"),)

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column()
    delete_media: Mapped[bool] = mapped_column(Boolean)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountDeliveryRow(IdentityBase):
    """Снимок получателя и отзываемая аренда одной durable-доставки."""

    __tablename__ = TableName.DELIVERIES
    __table_args__ = (
        UniqueConstraint("event_id", "recipient", "base_url", name="uq_identity_delivery_target"),
        Index("ix_identity_delivery_claim", "delivered_at", "next_attempt_at", "lease_until"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{TableName.OUTBOX}.event_id", ondelete="CASCADE")
    )
    recipient: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(2048))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_identity_deleted_owners", UserRow.id, postgresql_where=UserRow.state == UserState.DELETED)
