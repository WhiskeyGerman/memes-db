"""PostgreSQL-транзакции, row locks и надежная REST-очередь."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from memes_kernel.values import Number
from memes_processing.application.dto.coordinator import (
    CoordinatorLimit,
    EventDestination,
    OutgoingEvent,
    Payload,
    Source,
    SourceType,
    WorkerDevice,
    WorkItem,
)
from memes_processing.application.dto.features import integer, mapping
from memes_processing.application.ports.repository import (
    IProcessingAction,
    IProcessingRepository,
    ITransaction,
)
from memes_processing.application.tasks import TaskState
from memes_processing.infrastructure.config.settings import ProcessingDefault
from memes_processing.infrastructure.persistence.codec import (
    parse_source,
    parse_work,
    source_payload,
    work_payload,
)
from memes_processing.infrastructure.persistence.models import (
    AttemptRow,
    CleanupRow,
    InboxRow,
    OutboxRow,
    SourceRow,
    TaskRow,
    TombstoneRow,
    WorkerRow,
)


class PostgresRepository(IProcessingRepository):
    """Адаптер единственной транзакции координатора."""

    def __init__(self, session: AsyncSession) -> None:
        """Принять сессию, которой владеет unit of work."""
        self.session = session

    async def status(self, *, now: datetime, lease_seconds: int) -> Payload:
        """Прочитать нагрузку через собственные таблицы координатора."""
        counts: Payload = {}
        for source_type, key in (
            (SourceType.MEDIA, "pending_publication_tasks"),
            (SourceType.SAMPLE, "pending_sample_tasks"),
        ):
            count = await self.session.scalar(
                select(func.count())
                .select_from(TaskRow)
                .join(SourceRow)
                .where(
                    TaskRow.state.in_([TaskState.WAITING, TaskState.RETRY_WAIT, TaskState.RUNNING]),
                    SourceRow.data["type"].astext == source_type.value,
                )
            )
            counts[key] = count or Number.ZERO
        counts["available_workers"] = (
            await self.session.scalar(
                select(func.count())
                .select_from(WorkerRow)
                .where(
                    WorkerRow.revoked_at.is_(None),
                    WorkerRow.last_seen_at >= now - timedelta(seconds=lease_seconds),
                )
            )
            or Number.ZERO
        )
        return counts

    async def schedule_cleanup(self, key: str, *, ready_at: datetime) -> None:
        """Выдача права на загрузку и запись cleanup происходят атомарно."""
        await self.session.execute(
            insert(CleanupRow)
            .values(
                key=key,
                ready_at=ready_at,
            )
            .on_conflict_do_nothing()
        )

    async def retain_artifact(self, key: str) -> None:
        """Убрать принятый медиа-артефакт из технической очистки."""
        await self.session.execute(
            update(CleanupRow).where(CleanupRow.key == key).values(retained=True)
        )

    async def cleanup(
        self, *, now: datetime, limit: int = CoordinatorLimit.OUTBOX_BATCH
    ) -> list[str]:
        """Очистка не блокирует принятую публикацией область."""
        values = await self.session.scalars(
            select(CleanupRow.key)
            .where(CleanupRow.ready_at <= now, CleanupRow.retained.is_(False))
            .order_by(CleanupRow.ready_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(values)

    async def cleanup_done(self, key: str) -> None:
        """После быстрого удаления повторить его по окончании выданного PUT."""
        row = await self.session.scalar(select(CleanupRow).where(CleanupRow.key == key))
        if row is not None and row.final_at is not None and row.final_at > row.ready_at:
            row.ready_at = row.final_at
            row.final_at = None
        else:
            await self.session.execute(delete(CleanupRow).where(CleanupRow.key == key))

    async def lock_source(self, source_id: UUID) -> None:
        """Advisory lock не требует уже существующего оригинала."""
        identifier = int.from_bytes(source_id.bytes[:8], byteorder="big", signed=True)
        await self.session.execute(select(func.pg_advisory_xact_lock(identifier)))

    async def source_deleted(self, source_id: UUID) -> bool:
        """Tombstone проверяется независимо от обработки Published."""
        return (
            await self.session.scalar(
                select(TombstoneRow.source_id).where(TombstoneRow.source_id == source_id)
            )
            is not None
        )

    async def mark_deleted(self, source_id: UUID, *, now: datetime) -> None:
        """Стереть события с содержимым и ускорить надежное S3 удаление."""
        await self.session.execute(
            insert(TombstoneRow)
            .values(
                source_id=source_id,
                deleted_at=now,
            )
            .on_conflict_do_nothing()
        )
        await self.session.execute(delete(OutboxRow).where(OutboxRow.source_id == source_id))
        await self.session.execute(
            update(CleanupRow)
            .where(
                CleanupRow.key.startswith(f"attempts/{source_id}/"),
            )
            .values(
                retained=False,
                final_at=func.greatest(CleanupRow.ready_at, CleanupRow.final_at),
                ready_at=func.least(CleanupRow.ready_at, now),
            )
        )

    async def claim_event(self, event_id: UUID) -> bool:
        """Dedup выполняется до изменения состояния и откатывается вместе с ним."""
        statement = (
            insert(InboxRow).values(id=event_id).on_conflict_do_nothing().returning(InboxRow.id)
        )
        return (await self.session.scalar(statement)) is not None

    async def source(self, source_id: UUID, *, lock: bool = False) -> Source | None:
        """Получить оригинал с актуальной tombstone."""
        statement = select(SourceRow).where(SourceRow.id == source_id)
        if lock:
            statement = statement.with_for_update()
        row = await self.session.scalar(statement)
        return parse_source(row.data) if row is not None else None

    async def save_source(self, source: Source) -> None:
        """Сохранить версию оригинала атомарно."""
        values = {
            "id": source.id,
            "data": source_payload(source),
            "deleted_at": source.deleted_at,
            "expires_at": source.expires_at,
        }
        statement = (
            insert(SourceRow)
            .values(**values)
            .on_conflict_do_update(index_elements=[SourceRow.id], set_=values)
        )
        await self.session.execute(statement)
        await self.session.flush()

    async def work(self, task_id: UUID, *, lock: bool = False) -> WorkItem | None:
        """При переходе блокировать оригинал прежде задачи."""
        row = await self.session.scalar(select(TaskRow).where(TaskRow.id == task_id))
        if row is None:
            return None
        source = await self.source(row.source_id, lock=lock)
        if source is None:
            return None
        if lock:
            row = await self.session.scalar(
                select(TaskRow).where(TaskRow.id == task_id).with_for_update()
            )
            if row is None:
                return None
        return parse_work(row.data, source)

    async def save_work(self, work: WorkItem) -> None:
        """JSONB и индексируемые поля изменяются одной записью."""
        task = work.task
        values = {
            "id": task.id,
            "source_id": work.source.id,
            "kind": task.input.kind.value,
            "generation": work.generation,
            "state": task.state.value,
            "created_at": task.created_at,
            "ready_at": task.ready_at,
            "expires_at": task.lease.expires_at if task.lease else None,
            "worker_id": task.lease.worker_id if task.lease else None,
            "data": work_payload(work),
        }
        await self.session.execute(
            insert(TaskRow)
            .values(**values)
            .on_conflict_do_update(index_elements=[TaskRow.id], set_=values)
        )
        await self.session.flush()

    async def source_tasks(self, source_id: UUID) -> list[WorkItem]:
        """История этапов сохраняется по поколениям."""
        source = await self.source(source_id)
        if source is None:
            return []
        rows = await self.session.scalars(
            select(TaskRow)
            .where(TaskRow.source_id == source_id)
            .order_by(TaskRow.generation, TaskRow.created_at)
        )
        return [parse_work(row.data, source) for row in rows]

    async def available(
        self, *, worker_id: UUID, capabilities: Payload, now: datetime
    ) -> WorkItem | None:
        """Не тратить попытки на несовместимые модели или заполненное устройство."""
        maximum = integer(capabilities.get("maximum_parallel_tasks"))
        running = await self.session.scalar(
            select(func.count())
            .select_from(TaskRow)
            .where(TaskRow.worker_id == worker_id, TaskRow.state == TaskState.RUNNING)
        )
        if running is not None and running >= maximum:
            return None
        models = mapping(capabilities.get("versions"))
        if not models:
            return None
        compatible = or_(
            *(
                and_(TaskRow.kind == kind, TaskRow.data["version"] == mapping(version))
                for kind, version in models.items()
            )
        )
        statement = (
            select(TaskRow, SourceRow)
            .join(SourceRow, SourceRow.id == TaskRow.source_id)
            .where(
                TaskRow.state.in_([TaskState.WAITING, TaskState.RETRY_WAIT]),
                or_(TaskRow.ready_at.is_(None), TaskRow.ready_at <= now),
                SourceRow.deleted_at.is_(None),
                or_(SourceRow.expires_at.is_(None), SourceRow.expires_at > now),
                compatible,
            )
            .order_by(TaskRow.created_at)
            .limit(Number.ONE)
            .with_for_update(skip_locked=True)
        )
        matched = (await self.session.execute(statement)).first()
        if matched is None:
            return None
        task, source = matched
        return parse_work(task.data, parse_source(source.data))

    async def expired_sources(self, *, now: datetime) -> list[Source]:
        """Очистка приватного текста не зависит от наличия GPU воркера."""
        rows = await self.session.scalars(
            select(SourceRow)
            .where(
                SourceRow.deleted_at.is_(None),
                SourceRow.expires_at <= now,
            )
            .order_by(SourceRow.expires_at)
            .limit(CoordinatorLimit.OUTBOX_BATCH)
            .with_for_update(skip_locked=True)
        )
        return [parse_source(row.data) for row in rows]

    async def expired(self, *, now: datetime) -> list[WorkItem]:
        """Пропускать назначения, которыми уже занимается другой reaper."""
        statement = (
            select(TaskRow, SourceRow)
            .join(SourceRow, SourceRow.id == TaskRow.source_id)
            .where(
                TaskRow.state == TaskState.RUNNING,
                TaskRow.expires_at <= now,
            )
            .order_by(TaskRow.expires_at)
            .limit(CoordinatorLimit.OUTBOX_BATCH)
            .with_for_update(skip_locked=True)
        )
        return [
            parse_work(task.data, parse_source(source.data))
            for task, source in (await self.session.execute(statement)).all()
        ]

    async def revoked_sources(self, source_ids: tuple[UUID, ...], *, now: datetime) -> list[UUID]:
        """Одна индексированная выборка заменяет отдельный запрос на каждый файл."""
        if not source_ids:
            return []
        active = set(
            await self.session.scalars(
                select(SourceRow.id).where(
                    SourceRow.id.in_(source_ids),
                    SourceRow.deleted_at.is_(None),
                    or_(SourceRow.expires_at.is_(None), SourceRow.expires_at > now),
                )
            )
        )
        return [source_id for source_id in source_ids if source_id not in active]

    async def workers(self) -> list[WorkerDevice]:
        """Не выдавать неограниченное число устройств в одном REST ответе."""
        rows = await self.session.scalars(
            select(WorkerRow)
            .order_by(
                WorkerRow.created_at.desc(),
                WorkerRow.id,
            )
            .limit(CoordinatorLimit.OUTBOX_BATCH)
        )
        return [
            WorkerDevice(
                id=row.id,
                name=row.name,
                token_hash=row.token_hash,
                created_at=row.created_at,
                revoked_at=row.revoked_at,
                last_seen_at=row.last_seen_at,
                capabilities=row.capabilities,
            )
            for row in rows
        ]

    async def worker(self, worker_id: UUID, *, lock: bool = False) -> WorkerDevice | None:
        """Получить отзываемые машинные credentials."""
        statement = select(WorkerRow).where(WorkerRow.id == worker_id)
        if lock:
            statement = statement.with_for_update()
        row = await self.session.scalar(statement)
        if row is None:
            return None
        return WorkerDevice(
            id=row.id,
            name=row.name,
            token_hash=row.token_hash,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            last_seen_at=row.last_seen_at,
            capabilities=row.capabilities,
        )

    async def save_worker(self, worker: WorkerDevice) -> None:
        """Зарегистрировать устройство или обновить наблюдаемость."""
        values = {
            "id": worker.id,
            "name": worker.name,
            "token_hash": worker.token_hash,
            "created_at": worker.created_at,
            "revoked_at": worker.revoked_at,
            "last_seen_at": worker.last_seen_at,
            "capabilities": worker.capabilities,
        }
        await self.session.execute(
            insert(WorkerRow)
            .values(**values)
            .on_conflict_do_update(index_elements=[WorkerRow.id], set_=values)
        )

    async def attempt(
        self, work: WorkItem, *, state: str, finished_at: datetime | None = None
    ) -> None:
        """Сохранить конкретный fencing ID в истории."""
        lease = work.task.lease
        if lease is None:
            return
        values = {
            "id": lease.id,
            "task_id": work.task.id,
            "worker_id": lease.worker_id,
            "started_at": lease.started_at,
            "deadline": lease.deadline,
            "finished_at": finished_at,
            "state": state,
        }
        await self.session.execute(
            insert(AttemptRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[AttemptRow.id],
                set_={"finished_at": finished_at, "state": state},
            )
        )

    async def enqueue(self, event: OutgoingEvent) -> None:
        """Сохранить результат перед возвратом ACK исполнителю."""
        self.session.add(
            OutboxRow(
                id=event.id,
                source_id=event.source_id,
                destination=event.destination.value,
                payload=event.payload,
                created_at=event.created_at,
                attempts=event.attempts,
                ready_at=event.ready_at,
                delivered_at=None,
            )
        )
        await self.session.flush()

    async def outgoing(
        self, *, now: datetime, limit: int = CoordinatorLimit.OUTBOX_BATCH
    ) -> list[OutgoingEvent]:
        """Доставлять только первое событие каждого оригинала, сохраняя порядок."""
        earlier = aliased(OutboxRow)
        statement = (
            select(OutboxRow)
            .where(
                OutboxRow.delivered_at.is_(None),
                or_(OutboxRow.ready_at.is_(None), OutboxRow.ready_at <= now),
                ~exists(
                    select(earlier.id).where(
                        earlier.source_id == OutboxRow.source_id,
                        earlier.delivered_at.is_(None),
                        earlier.sequence < OutboxRow.sequence,
                    )
                ),
            )
            .order_by(OutboxRow.sequence)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = await self.session.scalars(statement)
        return [
            OutgoingEvent(
                id=row.id,
                destination=EventDestination(row.destination),
                source_id=row.source_id,
                payload=row.payload,
                created_at=row.created_at,
                attempts=row.attempts,
                ready_at=row.ready_at,
            )
            for row in rows
        ]

    async def acknowledge(self, event_id: UUID) -> None:
        """Подтвердить доставку в PostgreSQL."""
        await self.session.execute(
            update(OutboxRow).where(OutboxRow.id == event_id).values(delivered_at=datetime.now(UTC))
        )

    async def postpone(self, event: OutgoingEvent, *, now: datetime) -> None:
        """REST-события не отбрасываются после временного сбоя."""
        exponent = min(event.attempts, ProcessingDefault.OUTBOX_MAX_EXPONENT)
        delay = min(ProcessingDefault.OUTBOX_MAX_DELAY, ProcessingDefault.OUTBOX_FACTOR**exponent)
        await self.session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == event.id)
            .values(
                attempts=event.attempts + Number.ONE,
                ready_at=now + timedelta(seconds=delay),
            )
        )


class PostgresTransaction(ITransaction):
    """Явная транзакционная граница прикладного сценария."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Принять фабрику сессий инфраструктурного ресурса."""
        self.sessions = sessions

    async def run(self, action: IProcessingAction) -> Payload:
        """Коммитить ответ лишь после успешного выполнения сценария."""
        async with self.sessions() as session, session.begin():
            return await action.execute(PostgresRepository(session))
