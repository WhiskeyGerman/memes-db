"""SQL-адаптер долговечных leases без внешнего I/O в транзакции."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.delivery import DeliveryMessage, OutboxAction
from memes_search.application.ports.outbox import IOutboxRepository
from memes_search.infrastructure.persistence.codecs import SAMPLE
from memes_search.infrastructure.persistence.models import OutboxRow


class PostgreSQLOutbox(IOutboxRepository):
    """Конкурентный REST-outbox с короткими SQL-транзакциями."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Сохранить фабрику транзакций."""
        self._factory = factory

    async def claim(
        self, now: datetime, lease_until: datetime, limit: int
    ) -> tuple[DeliveryMessage, ...]:
        """Зафиксировать захват до возврата сообщения приложению."""
        async with self._factory() as session, session.begin():
            rows = (
                await session.scalars(
                    select(OutboxRow)
                    .where(
                        OutboxRow.available_at <= now,
                        or_(OutboxRow.claimed_until.is_(None), OutboxRow.claimed_until <= now),
                    )
                    .order_by(OutboxRow.available_at, OutboxRow.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            messages: list[DeliveryMessage] = []
            for row in rows:
                row.claim_id = uuid4()
                row.claimed_until = lease_until
                action = OutboxAction(row.action)
                messages.append(
                    DeliveryMessage(
                        id=row.id,
                        claim_id=row.claim_id,
                        action=action,
                        attempts=row.attempts,
                        sample=SAMPLE.validate_python(row.body)
                        if action is OutboxAction.SUBMIT
                        else None,
                        sample_id=UUID(
                            str(
                                row.body["sample_id"] if "sample_id" in row.body else row.body["id"]
                            )
                        )
                        if action is OutboxAction.CANCEL
                        else None,
                        task_id=row.task_id,
                        object_key=str(row.body["object_key"])
                        if action is OutboxAction.DELETE_OBJECT
                        else None,
                    )
                )
            return tuple(messages)

    async def acknowledge(self, message_id: UUID, claim_id: UUID) -> None:
        """Защитить новую доставку от ACK истёкшего lease."""
        async with self._factory() as session, session.begin():
            await session.execute(
                delete(OutboxRow).where(OutboxRow.id == message_id, OutboxRow.claim_id == claim_id)
            )

    async def retry(
        self, message_id: UUID, claim_id: UUID, attempts: int, available_at: datetime
    ) -> None:
        """Освободить актуальный lease после ошибки получателя."""
        async with self._factory() as session, session.begin():
            await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == message_id, OutboxRow.claim_id == claim_id)
                .values(
                    attempts=attempts,
                    available_at=available_at,
                    claim_id=None,
                    claimed_until=None,
                )
            )
