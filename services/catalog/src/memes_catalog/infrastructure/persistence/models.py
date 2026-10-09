"""SQLAlchemy-модели PostgreSQL; не доменные сущности."""

from datetime import datetime
from enum import StrEnum
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from memes_catalog.domain.voting import BallotState
from memes_platform.application.dto.json import JsonObject


class TableName(StrEnum):
    """Имена принадлежащих Catalog таблиц."""

    MEDIA = "catalog_media"
    GROUPS = "catalog_groups"
    MEMBERSHIPS = "catalog_memberships"
    BALLOTS = "catalog_ballots"
    VOTES = "catalog_votes"
    DECISIONS = "catalog_decisions"
    DISMISSALS = "catalog_dismissals"
    UPLOADS = "catalog_uploads"
    OUTBOX = "catalog_outbox"
    INBOX = "catalog_inbox"
    CLEANUP = "catalog_cleanup"
    ACCOUNTS = "catalog_account_tombstones"
    RATE = "catalog_vote_rate"
    FEATURES = "catalog_features"


class IndexName(StrEnum):
    """Явные имена ограничений и индексов."""

    MEDIA_HASH = "uq_catalog_active_sha256"
    MEMBERSHIP = "uq_catalog_active_membership"
    VOTE = "uq_catalog_ballot_voter"
    BALLOT = "uq_catalog_open_target"
    DISMISSAL = "uq_catalog_personal_dismissal"
    MEDIA_FEED = "ix_catalog_media_feed"
    OUTBOX_READY = "ix_catalog_outbox_ready"
    CLEANUP_OBJECT = "uq_catalog_cleanup_object"


class Base(DeclarativeBase):
    """Метаданные единственного владельца таблиц."""


class MediaRow(Base):
    """Сохраняемое представление уникальной публикации."""

    __tablename__ = TableName.MEDIA

    id: Mapped[UUID] = mapped_column(primary_key=True)
    author_id: Mapped[UUID] = mapped_column(index=True)
    file_id: Mapped[UUID]
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    format: Mapped[str]
    kind: Mapped[str]
    metadata_payload: Mapped[JsonObject] = mapped_column(JSONB)
    object_key: Mapped[str]
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int]
    projection_revision: Mapped[int]

    __table_args__ = (
        Index(
            IndexName.MEDIA_HASH,
            sha256,
            unique=True,
            postgresql_where=text("state = 'published'"),
        ),
        Index(IndexName.MEDIA_FEED, "published_at", "id"),
    )


class GroupRow(Base):
    """Состав группы с отдельной ревизией."""

    __tablename__ = TableName.GROUPS

    id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int]
    composition_revision: Mapped[int]
    member_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str]
    successor_id: Mapped[UUID | None]


class MembershipRow(Base):
    """Исторический эпизод принадлежности."""

    __tablename__ = TableName.MEMBERSHIPS

    id: Mapped[UUID] = mapped_column(primary_key=True)
    media_id: Mapped[UUID] = mapped_column(ForeignKey(f"{TableName.MEDIA}.id"), index=True)
    group_id: Mapped[UUID] = mapped_column(ForeignKey(f"{TableName.GROUPS}.id"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None]
    revision: Mapped[int]

    __table_args__ = (
        Index(IndexName.MEMBERSHIP, media_id, unique=True, postgresql_where=ended_at.is_(None)),
    )


class BallotRow(Base):
    """Счётчик и неизменяемая цель голосования."""

    __tablename__ = TableName.BALLOTS

    id: Mapped[UUID] = mapped_column(primary_key=True)
    target_key: Mapped[str]
    target_payload: Mapped[JsonObject] = mapped_column(JSONB)
    threshold: Mapped[int]
    policy_revision: Mapped[UUID]
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active_votes: Mapped[int]
    state: Mapped[str]
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_id: Mapped[UUID | None]
    revision: Mapped[int]


Index(
    IndexName.BALLOT,
    BallotRow.target_key,
    unique=True,
    postgresql_where=BallotRow.state.in_((BallotState.OPEN, BallotState.REACHED)),
)


class VoteRow(Base):
    """Единственный голос аккаунта в ballot."""

    __tablename__ = TableName.VOTES

    id: Mapped[UUID] = mapped_column(primary_key=True)
    ballot_id: Mapped[UUID] = mapped_column(ForeignKey(f"{TableName.BALLOTS}.id"), index=True)
    voter_id: Mapped[UUID]
    cast_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int]

    __table_args__ = (UniqueConstraint("ballot_id", "voter_id", name=IndexName.VOTE),)


class DecisionRow(Base):
    """Неизменяемый исторический факт исполнения."""

    __tablename__ = TableName.DECISIONS

    id: Mapped[UUID] = mapped_column(primary_key=True)
    ballot_id: Mapped[UUID] = mapped_column(index=True)
    payload: Mapped[JsonObject] = mapped_column(JSONB)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DismissalRow(Base):
    """Личное отклонение конкретного состава."""

    __tablename__ = TableName.DISMISSALS

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    media_id: Mapped[UUID]
    group_id: Mapped[UUID]
    group_revision: Mapped[int]
    dismissed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "user_id", "media_id", "group_id", "group_revision", name=IndexName.DISMISSAL
        ),
    )


class UploadRow(Base):
    """Длительная загрузка с серверной квотой и lease приёмки."""

    __tablename__ = TableName.UPLOADS

    id: Mapped[UUID] = mapped_column(primary_key=True)
    author_id: Mapped[UUID] = mapped_column(index=True)
    file_id: Mapped[UUID]
    media_id: Mapped[UUID]
    object_key: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str]
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_media_id: Mapped[UUID | None]
    claim_id: Mapped[UUID | None]


class FeatureRow(Base):
    """Последнее поколение одного независимого признака."""

    __tablename__ = TableName.FEATURES

    media_id: Mapped[UUID] = mapped_column(ForeignKey(f"{TableName.MEDIA}.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(primary_key=True)
    cycle_id: Mapped[UUID]
    generation: Mapped[int]
    state: Mapped[str]
    feature_version: Mapped[JsonObject] = mapped_column(JSONB)
    result: Mapped[JsonObject | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxRow(Base):
    """Durable REST-доставка с правом владения диспетчера."""

    __tablename__ = TableName.OUTBOX

    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    target: Mapped[str]
    payload: Mapped[JsonObject] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int]
    claim_id: Mapped[UUID | None]
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InboxRow(Base):
    """Журнал идемпотентной приёмки событий."""

    __tablename__ = TableName.INBOX

    id: Mapped[UUID] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CleanupRow(Base):
    """Durable-задание физического удаления."""

    __tablename__ = TableName.CLEANUP

    id: Mapped[UUID] = mapped_column(primary_key=True)
    object_key: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountTombstoneRow(Base):
    """Обезличенный выбор удаления без прежнего логина."""

    __tablename__ = TableName.ACCOUNTS

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    delete_media: Mapped[bool] = mapped_column(Boolean)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VoteRateRow(Base):
    """Серверный счётчик действий за минуту."""

    __tablename__ = TableName.RATE

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    window: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer)
