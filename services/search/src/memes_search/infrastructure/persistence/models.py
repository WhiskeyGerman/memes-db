"""Отображение таблиц Search; бизнес-модели не зависят от ORM."""

from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Метаданные только собственного Search-хранилища."""


class MediaRow(Base):
    """Восстанавливаемые публичность и членство с вечным tombstone."""

    __tablename__ = "search_media"

    media_id: Mapped[UUID] = mapped_column(primary_key=True)
    group_id: Mapped[UUID | None]
    group_revision: Mapped[int | None]
    revision: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean)
    media_kind: Mapped[str | None] = mapped_column(String)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeatureRow(Base):
    """Последнее поколение независимого поискового признака."""

    __tablename__ = "search_features"

    media_id: Mapped[UUID] = mapped_column(ForeignKey("search_media.media_id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer)
    cycle_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String)
    version_key: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)


class VectorRow(Base):
    """Фрагментный вектор с явно совместимым пространством."""

    __tablename__ = "search_vectors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["media_id", "kind"],
            ["search_features.media_id", "search_features.kind"],
            ondelete="CASCADE",
        ),
    )

    media_id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    space_id: Mapped[str] = mapped_column(String)
    values: Mapped[list[float]] = mapped_column(Vector())
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)


class TextFragmentRow(Base):
    """Текстовый фрагмент для выбора таймкода по релевантности."""

    __tablename__ = "search_text_fragments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["media_id", "kind"],
            ["search_features.media_id", "search_features.kind"],
            ondelete="CASCADE",
        ),
    )

    media_id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)


class InboxRow(Base):
    """Ключи уже доставленных событий."""

    __tablename__ = "search_inbox"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionRow(Base):
    """Снимок выдачи и зашифрованный транспортом приватный запрос."""

    __tablename__ = "search_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    body: Mapped[dict[str, object]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RequestRow(Base):
    """Сохранённый запрос асинхронного inference."""

    __tablename__ = "search_requests"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    body: Mapped[dict[str, object]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SampleRow(Base):
    """Владение и квота приватного образца."""

    __tablename__ = "search_samples"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(index=True)
    state: Mapped[str] = mapped_column(String)
    body: Mapped[dict[str, object]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SampleFeatureRow(Base):
    """Последнее поколение признака живого образца."""

    __tablename__ = "search_sample_features"

    sample_id: Mapped[UUID] = mapped_column(
        ForeignKey("search_samples.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict[str, object]] = mapped_column(JSONB)


class OutboxRow(Base):
    """Персистентная очередь REST-доставки координатору обработки."""

    __tablename__ = "search_outbox"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sample_id: Mapped[UUID | None]
    task_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String)
    body: Mapped[dict[str, object]] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    claim_id: Mapped[UUID | None]
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeletedOwnerRow(Base):
    """Постоянный fence без логина, текста или признаков."""

    __tablename__ = "search_deleted_owners"

    owner_id: Mapped[UUID] = mapped_column(primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
