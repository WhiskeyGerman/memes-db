"""PostgreSQL-исполнение очистки Identity без прикладной retention политики."""

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.ports.maintenance import IIdentityMaintenance
from memes_identity.infrastructure.persistence.models import (
    AccountOutboxRow,
    RateBucketRow,
    SessionRow,
)


class SqlIdentityMaintenance(IIdentityMaintenance):
    """Атомарная очистка технических строк по переданным границам."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Получить фабрику отдельной административной транзакции.

        Args:
            factory: Фабрика PostgreSQL-сессий.
        """
        self._factory = factory

    async def purge(
        self, *, sessions_before: datetime, rates_before: datetime, deliveries_before: datetime
    ) -> None:
        """Удалить истёкшие сессии, старые окна и доставленные уведомления."""
        async with self._factory.begin() as session:
            await session.execute(
                delete(SessionRow).where(SessionRow.expires_at <= sessions_before)
            )
            await session.execute(
                delete(RateBucketRow).where(RateBucketRow.window_started_at <= rates_before)
            )
            await session.execute(
                delete(AccountOutboxRow).where(AccountOutboxRow.delivered_at <= deliveries_before)
            )
