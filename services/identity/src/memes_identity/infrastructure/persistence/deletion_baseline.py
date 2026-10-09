"""PostgreSQL snapshot удалённых идентичностей для candidate Search."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.dto.deletion_baseline import DeletedOwner
from memes_identity.application.ports.deletion_baseline import IDeletedOwnerSource
from memes_identity.domain.user import UserId, UserState
from memes_identity.infrastructure.persistence.models import UserRow


class SqlDeletedOwnerSource(IDeletedOwnerSource):
    """Ограниченное keyset-чтение технических tombstone из Identity."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Получить фабрику коротких read-only транзакций.

        Args:
            factory: Фабрика PostgreSQL-сессий.
        """
        self._factory = factory

    async def page(self, *, after: UserId | None, limit: int) -> tuple[DeletedOwner, ...]:
        """Выбрать только ID и время окончательного удаления.

        Args:
            after: Последний UUID предыдущей страницы.
            limit: Граница памяти административного процесса.
        """
        statement = (
            select(UserRow.id, UserRow.deleted_at)
            .where(UserRow.state == UserState.DELETED, UserRow.deleted_at.is_not(None))
            .order_by(UserRow.id)
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(UserRow.id > after)
        async with self._factory() as session:
            rows = await session.execute(statement)
            return tuple(
                DeletedOwner(user_id=UserId(identifier), deleted_at=deleted_at)
                for identifier, deleted_at in rows
            )
