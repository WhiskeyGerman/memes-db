"""Таблицы состояния координатора; чужие схемы сервис недоступны."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from memes_processing.application.dto.coordinator import JSONValue


class Base(DeclarativeBase):
    """Метаданные принадлежащих сервису таблиц."""


class SourceRow(Base):
    """Версия оригинала и сроки жизни приватного образца."""

    __tablename__ = "processing_sources"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    data: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskRow(Base):
    """Durable задача с полями для индексированного scheduling."""

    __tablename__ = "processing_tasks"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("processing_sources.id"))
    kind: Mapped[str] = mapped_column(String(32))
    generation: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    data: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("source_id", "kind", "generation"),
        Index("ix_processing_tasks_queue", "state", "ready_at", "created_at"),
        Index("ix_processing_tasks_lease", "expires_at"),
        Index("ix_processing_tasks_worker", "worker_id", "state"),
    )


class WorkerRow(Base):
    """Доверенное устройство с индивидуальным отпечатком секрета."""

    __tablename__ = "processing_workers"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capabilities: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)


class AttemptRow(Base):
    """История назначений, включая утрату lease."""

    __tablename__ = "processing_attempts"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("processing_tasks.id"))
    worker_id: Mapped[UUID] = mapped_column(ForeignKey("processing_workers.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32))


class InboxRow(Base):
    """Транзакционный dedup входящих событий."""

    __tablename__ = "processing_inbox"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)


class OutboxRow(Base):
    """Упорядоченные REST-уведомления с независимыми повторами."""

    __tablename__ = "processing_outbox"
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    destination: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, JSONValue]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_processing_outbox_ready", "delivered_at", "ready_at", "created_at"),
        Index("ix_processing_outbox_source_sequence", "source_id", "sequence"),
    )


class CleanupRow(Base):
    """Durable удаление артефактов после истечения доступа на запись."""

    __tablename__ = "processing_artifact_cleanup"
    key: Mapped[str] = mapped_column(String(1024), primary_key=True)
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retained: Mapped[bool] = mapped_column(Boolean, default=False)
    final_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TombstoneRow(Base):
    """Отмена неизвестного источника блокирует позднюю публикацию."""

    __tablename__ = "processing_tombstones"
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
