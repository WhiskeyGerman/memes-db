"""PostgreSQL атомарный admission ledger первичного Search."""

from datetime import datetime
from enum import IntEnum
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_kernel.values import Number
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.sample_admission_ledger import ISampleAdmissionLedger
from memes_search.infrastructure.persistence.models import DeletedOwnerRow
from memes_search.infrastructure.persistence.owner_guard import lock_owner, require_active_owner
from memes_search.infrastructure.persistence.sample_admission_models import SampleAdmissionRow


class AdmissionBatch(IntEnum):
    """Граница одной технической GC транзакции ledger."""

    PURGE = 1000


class PostgreSQLSampleAdmission(ISampleAdmissionLedger):
    """Сериализовать primary и REST shadow reservations единым owner lock."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Получить фабрику коротких транзакций.

        Args:
            factory: Primary Search PostgreSQL.
        """
        self._factory = factory

    async def reserve(
        self,
        *,
        sample_id: UUID,
        owner_id: UUID,
        created_at: datetime,
        expires_at: datetime,
        now: datetime,
        limit: int,
    ) -> None:
        """Проверить общий предел и fence атомарно; retry не продлевает резерв.

        Args:
            sample_id: Устойчивый ID.
            owner_id: Владелец.
            created_at: Проверенное время создания.
            expires_at: Проверенный срок.
            now: Время Application.
            limit: Серверный предел из Application.
        """
        async with self._factory() as session, session.begin():
            await lock_owner(session, owner_id)
            await require_active_owner(session, owner_id)
            existing = await session.get(SampleAdmissionRow, sample_id)
            if existing is not None:
                if existing.owner_id != owner_id:
                    raise SearchError(SearchErrorCode.FORBIDDEN)
                if existing.created_at != created_at or existing.expires_at != expires_at:
                    raise SearchError(SearchErrorCode.INVALID_SAMPLE)
                return
            used = await session.scalar(
                select(func.count())
                .select_from(SampleAdmissionRow)
                .where(SampleAdmissionRow.owner_id == owner_id, SampleAdmissionRow.expires_at > now)
            )
            if int(used or Number.ZERO) >= limit:
                raise SearchError(SearchErrorCode.SAMPLE_LIMIT)
            inserted = await session.scalar(
                insert(SampleAdmissionRow)
                .values(
                    sample_id=sample_id,
                    owner_id=owner_id,
                    created_at=created_at,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(index_elements=[SampleAdmissionRow.sample_id])
                .returning(SampleAdmissionRow.sample_id)
            )
            if inserted is None:
                raise SearchError(SearchErrorCode.FORBIDDEN)

    async def release(self, sample_id: UUID) -> None:
        """Освободить один слот под каноническим owner lock.

        Args:
            sample_id: ID терминального образца.
        """
        async with self._factory() as session, session.begin():
            owner = await session.scalar(
                select(SampleAdmissionRow.owner_id).where(SampleAdmissionRow.sample_id == sample_id)
            )
            if owner is not None:
                await lock_owner(session, owner)
                await session.execute(
                    delete(SampleAdmissionRow).where(
                        SampleAdmissionRow.sample_id == sample_id,
                        SampleAdmissionRow.owner_id == owner,
                    )
                )

    async def erase_owner(self, owner_id: UUID, now: datetime) -> None:
        """Запретить поздний reserve и снять все слоты в одной транзакции.

        Args:
            owner_id: Удалённый владелец.
            now: Время owner fence.
        """
        async with self._factory() as session, session.begin():
            await lock_owner(session, owner_id)
            await session.execute(
                insert(DeletedOwnerRow)
                .values(
                    owner_id=owner_id,
                    deleted_at=now,
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                delete(SampleAdmissionRow).where(SampleAdmissionRow.owner_id == owner_id)
            )

    async def purge(self, now: datetime) -> None:
        """Удалить истёкшие технические слоты без содержимого private samples.

        Args:
            now: Серверная граница TTL.
        """
        async with self._factory() as session, session.begin():
            expired = (
                select(SampleAdmissionRow.sample_id)
                .where(SampleAdmissionRow.expires_at <= now)
                .order_by(SampleAdmissionRow.expires_at, SampleAdmissionRow.sample_id)
                .limit(AdmissionBatch.PURGE)
                .with_for_update(skip_locked=True)
                .cte("admission_expired_batch")
            )
            await session.execute(
                delete(SampleAdmissionRow).where(
                    SampleAdmissionRow.sample_id.in_(select(expired.c.sample_id))
                )
            )
