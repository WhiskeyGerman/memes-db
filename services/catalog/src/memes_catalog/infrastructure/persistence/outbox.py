"""PostgreSQL lease, FIFO и backoff исходящих событий."""

from datetime import datetime, timedelta
from enum import IntEnum
from uuid import UUID, uuid4

from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from memes_catalog.application.dto.catalog import CatalogDefault, OutgoingEvent
from memes_catalog.application.ports.outbox import IOutboxRepository
from memes_catalog.infrastructure.persistence.models import OutboxRow
from memes_kernel.values import Number


class OutboxLimit(IntEnum):
    """Ограничения права владения и задержки повторов."""

    LEASE_SECONDS = 120
    BACKOFF_EXPONENT = 6
    MAXIMUM_BACKOFF_SECONDS = 300


class PostgresOutbox(IOutboxRepository):
    """Конкурентно безопасная очередь без брокера."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Сохранить фабрику независимых транзакций.

        Args:
            sessions: PostgreSQL-сессии.
        """
        self.sessions = sessions

    async def acquire(self, *, now: datetime, limit: int) -> tuple[OutgoingEvent, ...]:
        """Заблокировать готовые головы каждого REST-потока."""
        prior = aliased(OutboxRow)
        older = exists(
            select(prior.id).where(
                prior.target == OutboxRow.target,
                prior.payload["media_id"].astext == OutboxRow.payload["media_id"].astext,
                prior.sequence < OutboxRow.sequence,
                prior.delivered_at.is_(None),
            )
        )
        async with self.sessions.begin() as session:
            rows = await session.scalars(
                select(OutboxRow)
                .where(
                    OutboxRow.delivered_at.is_(None),
                    OutboxRow.ready_at <= now,
                    or_(OutboxRow.claimed_until.is_(None), OutboxRow.claimed_until <= now),
                    ~older,
                )
                .order_by(OutboxRow.sequence)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            events = []
            for row in rows:
                row.claim_id = uuid4()
                row.claimed_until = now + timedelta(seconds=OutboxLimit.LEASE_SECONDS)
                events.append(
                    OutgoingEvent(
                        id=row.id,
                        target=row.target,
                        payload=row.payload,
                        attempts=row.attempts,
                        claim_id=row.claim_id,
                    )
                )
            return tuple(events)

    async def acknowledge(self, event_id: UUID, claim_id: UUID) -> None:
        """Подтвердить сохранённый claim."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == event_id, OutboxRow.claim_id == claim_id)
                .values(delivered_at=datetime.now().astimezone(), claimed_until=None, claim_id=None)
            )

    async def retry(self, event: OutgoingEvent, *, now: datetime) -> None:
        """Сохранить ограниченный exponential backoff."""
        exponent = min(event.attempts, OutboxLimit.BACKOFF_EXPONENT)
        delay = min(
            CatalogDefault.OUTBOX_RETRY_SECONDS * (Number.ONE + Number.ONE) ** exponent,
            OutboxLimit.MAXIMUM_BACKOFF_SECONDS,
        )
        async with self.sessions.begin() as session:
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == event.id, OutboxRow.claim_id == event.claim_id)
                .values(
                    ready_at=now + timedelta(seconds=delay),
                    attempts=event.attempts + Number.ONE,
                    claim_id=None,
                    claimed_until=None,
                )
            )
