"""Атомарный fence и стирание приватных данных владельца."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_kernel.values import Number
from memes_search.application.dto.delivery import OutboxAction
from memes_search.application.dto.samples import SampleState
from memes_search.application.ports.private_data import IPrivateDataRepository
from memes_search.infrastructure.persistence.codecs import SAMPLE, sample_body
from memes_search.infrastructure.persistence.models import (
    DeletedOwnerRow,
    OutboxRow,
    RequestRow,
    SampleFeatureRow,
    SampleRow,
    SessionRow,
)
from memes_search.infrastructure.persistence.owner_guard import lock_owner


class PostgreSQLPrivateData(IPrivateDataRepository):
    """Сериализовать удаление со всеми поздними записями пользователя."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Сохранить фабрику единой транзакции удаления."""
        self._factory = factory

    async def erase_owner(self, owner_id: UUID, now: datetime) -> None:
        """Зафиксировать tombstone, отмену ML и стирание JSON атомарно."""
        async with self._factory() as session, session.begin():
            await lock_owner(session, owner_id)
            await session.execute(
                insert(DeletedOwnerRow)
                .values(owner_id=owner_id, deleted_at=now)
                .on_conflict_do_nothing()
            )
            samples = (
                await session.scalars(
                    select(SampleRow).where(SampleRow.owner_id == owner_id).with_for_update()
                )
            ).all()
            for row in samples:
                sample = replace(
                    SAMPLE.validate_python(row.body), state=SampleState.CANCELLED, expires_at=now
                )
                row.body, row.state, row.expires_at = sample_body(sample), sample.state, now
                await session.execute(
                    delete(SampleFeatureRow).where(SampleFeatureRow.sample_id == sample.id)
                )
                await session.execute(delete(OutboxRow).where(OutboxRow.sample_id == sample.id))
                session.add(
                    OutboxRow(
                        id=uuid4(),
                        action=OutboxAction.CANCEL,
                        body={"sample_id": str(sample.id)},
                        attempts=Number.ZERO,
                        available_at=now,
                    )
                )
            requests = (
                await session.scalars(
                    select(RequestRow)
                    .where(RequestRow.body["query"]["owner_id"].astext == str(owner_id))
                    .with_for_update()
                )
            ).all()
            for request in requests:
                session.add(
                    OutboxRow(
                        id=uuid4(),
                        task_id=request.id,
                        action=OutboxAction.CANCEL_QUERY,
                        body={},
                        attempts=Number.ZERO,
                        available_at=now,
                    )
                )
                await session.delete(request)
            await session.execute(
                delete(SessionRow).where(
                    SessionRow.body["query"]["owner_id"].astext == str(owner_id)
                )
            )
