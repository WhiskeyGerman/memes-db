"""Квоты, временные признаки и REST-outbox в PostgreSQL."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_kernel.values import Number
from memes_search.application.dto.delivery import OutboxAction
from memes_search.application.dto.projections import ProjectionState
from memes_search.application.dto.samples import (
    PrivateSample,
    SampleFeatureChange,
    SampleFeatures,
    SampleState,
)
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.samples import ISampleRepository
from memes_search.infrastructure.persistence.codecs import (
    SAMPLE,
    SAMPLE_FEATURE,
    sample_body,
    sample_feature_body,
)
from memes_search.infrastructure.persistence.models import (
    InboxRow,
    OutboxRow,
    SampleFeatureRow,
    SampleRow,
)
from memes_search.infrastructure.persistence.owner_guard import lock_owner, require_active_owner


class PostgreSQLSamples(ISampleRepository):
    """Атомарный учёт образцов и источников REST-доставки."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Сохранить фабрику транзакционных сессий."""
        self._factory = factory

    async def reserve(self, sample: PrivateSample, limit: int) -> None:
        """Сериализовать квоту владельца до приёма байтов файла."""
        async with self._factory() as session, session.begin():
            await lock_owner(session, sample.owner_id)
            await require_active_owner(session, sample.owner_id)
            count = await session.scalar(
                select(func.count())
                .select_from(SampleRow)
                .where(
                    SampleRow.owner_id == sample.owner_id,
                    SampleRow.expires_at > sample.created_at,
                    SampleRow.state.in_((SampleState.UPLOADING, SampleState.QUEUED)),
                )
            )
            if count is not None and count >= limit:
                raise SearchError(SearchErrorCode.SAMPLE_LIMIT)
            session.add(
                OutboxRow(
                    id=uuid4(),
                    action=OutboxAction.DELETE_OBJECT,
                    body={"object_key": sample.object_key},
                    attempts=Number.ZERO,
                    available_at=sample.expires_at,
                )
            )
            session.add(
                SampleRow(
                    id=sample.id,
                    owner_id=sample.owner_id,
                    state=sample.state,
                    body=sample_body(sample),
                    expires_at=sample.expires_at,
                )
            )

    async def get(self, sample_id: UUID) -> PrivateSample | None:
        """Прочитать принадлежащее Search состояние образца."""
        async with self._factory() as session:
            row = await session.get(SampleRow, sample_id)
            return SAMPLE.validate_python(row.body) if row is not None else None

    async def save(self, sample: PrivateSample, *, enqueue: bool = False) -> None:
        """Сохранить переход и outbox в одной транзакции."""
        async with self._factory() as session, session.begin():
            await lock_owner(session, sample.owner_id)
            await require_active_owner(session, sample.owner_id)
            row = await session.scalar(
                select(SampleRow).where(SampleRow.id == sample.id).with_for_update()
            )
            if row is None:
                raise SearchError(SearchErrorCode.NOT_FOUND)
            if row.state == SampleState.CANCELLED and sample.state is not SampleState.CANCELLED:
                return
            row.state = sample.state
            row.body = sample_body(sample)
            row.expires_at = sample.expires_at
            if enqueue:
                action = (
                    OutboxAction.CANCEL
                    if sample.state is SampleState.CANCELLED
                    else OutboxAction.SUBMIT
                )
                session.add(
                    OutboxRow(
                        id=uuid4(),
                        sample_id=sample.id if action is OutboxAction.SUBMIT else None,
                        action=action,
                        body=sample_body(sample)
                        if action is OutboxAction.SUBMIT
                        else {"sample_id": str(sample.id)},
                        attempts=Number.ZERO,
                        available_at=datetime.now(UTC),
                    )
                )

    async def features(self, sample_id: UUID) -> SampleFeatures:
        """Собрать независимые готовые признаки образца."""
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(SampleFeatureRow).where(SampleFeatureRow.sample_id == sample_id)
                )
            ).all()
            changes = tuple(SAMPLE_FEATURE.validate_python(row.body) for row in rows)
            return SampleFeatures(
                text=" ".join(item.text for item in changes if item.text),
                vectors=tuple(vector for item in changes for vector in item.vectors),
            )

    async def apply_features(self, change: SampleFeatureChange, now: datetime) -> bool:
        """Принять pending→terminal только текущего цикла."""
        async with self._factory() as session, session.begin():
            owner_id = await session.scalar(
                select(SampleRow.owner_id).where(SampleRow.id == change.sample_id)
            )
            if owner_id is None:
                return False
            await lock_owner(session, owner_id)
            try:
                await require_active_owner(session, owner_id)
            except SearchError:
                return False
            row = await session.scalar(
                select(SampleRow).where(SampleRow.id == change.sample_id).with_for_update()
            )
            if row is None or row.expires_at <= now or row.state == SampleState.CANCELLED:
                return False
            claimed = await session.scalar(
                insert(InboxRow)
                .values(event_id=change.event_id, received_at=now)
                .on_conflict_do_nothing(index_elements=[InboxRow.event_id])
                .returning(InboxRow.event_id)
            )
            if claimed is None:
                return False
            existing = await session.get(SampleFeatureRow, (change.sample_id, change.kind))
            update_content = True
            if existing is not None:
                previous = SAMPLE_FEATURE.validate_python(existing.body)
                if previous.generation > change.generation:
                    return False
                if previous.generation == change.generation:
                    if (
                        previous.cycle_id != change.cycle_id
                        or previous.version_key != change.version_key
                    ):
                        return False
                    if previous.state != ProjectionState.PENDING:
                        if previous.state != change.state or change.sample_state is None:
                            return False
                        update_content = False
                    elif change.state == ProjectionState.PENDING:
                        return False
            if update_content:
                if existing is None:
                    session.add(
                        SampleFeatureRow(
                            sample_id=change.sample_id,
                            kind=change.kind,
                            generation=change.generation,
                            body=sample_feature_body(change),
                        )
                    )
                else:
                    existing.generation = change.generation
                    existing.body = sample_feature_body(change)
            sample = SAMPLE.validate_python(row.body)
            if change.sample_state is SampleState.READY or change.text or change.vectors:
                sample = replace(sample, state=SampleState.READY, failure=None)
            elif (
                change.sample_state is SampleState.FAILED and sample.state is not SampleState.READY
            ):
                sample = replace(
                    sample, state=SampleState.FAILED, failure=SearchErrorCode.SAMPLE_FAILED
                )
            else:
                return update_content
            changed_state = row.state != sample.state
            row.state, row.body = sample.state, sample_body(sample)
            return update_content or changed_state

    async def expired(self, now: datetime, limit: int) -> tuple[PrivateSample, ...]:
        """Получить ограниченный пакет серверных файлов для удаления."""
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(SampleRow)
                    .where(SampleRow.expires_at <= now)
                    .order_by(SampleRow.expires_at)
                    .limit(limit)
                )
            ).all()
            return tuple(SAMPLE.validate_python(row.body) for row in rows)

    async def remove(self, sample_id: UUID) -> None:
        """Удалить признаки и очистить приватную запись после удаления файла."""
        async with self._factory() as session, session.begin():
            await session.execute(delete(SampleRow).where(SampleRow.id == sample_id))
            await session.execute(
                delete(OutboxRow).where(
                    OutboxRow.sample_id == sample_id, OutboxRow.action != OutboxAction.CANCEL
                )
            )

    async def schedule_cleanup(self, object_key: str, available_at: datetime) -> None:
        """Оставить независимую от владельца задачу стирания файла."""
        async with self._factory() as session, session.begin():
            session.add(
                OutboxRow(
                    id=uuid4(),
                    action=OutboxAction.DELETE_OBJECT,
                    body={"object_key": object_key},
                    attempts=Number.ZERO,
                    available_at=available_at,
                )
            )

    async def schedule_cancel(self, sample_id: UUID, available_at: datetime) -> None:
        """Поставить техническую отмену, переживающую удаление SQL и S3."""
        async with self._factory() as session, session.begin():
            await session.execute(
                insert(OutboxRow)
                .values(
                    id=sample_id,
                    action=OutboxAction.CANCEL,
                    body={"sample_id": str(sample_id)},
                    attempts=Number.ZERO,
                    available_at=available_at,
                )
                .on_conflict_do_nothing(index_elements=[OutboxRow.id])
            )
