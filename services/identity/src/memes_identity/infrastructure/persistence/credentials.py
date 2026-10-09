"""PostgreSQL-адаптер парольных verifier."""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from memes_identity.application.ports.passwords import ICredentialRepository
from memes_identity.domain.user import UserId
from memes_identity.infrastructure.persistence.models import CredentialRow


class SqlCredentialRepository(ICredentialRepository):
    """Хранилище verifier в транзакции изменения аккаунта."""

    def __init__(self, session: AsyncSession) -> None:
        """Получить текущую транзакцию.

        Args:
            session: Сессия PostgreSQL.
        """
        self._session = session

    async def get(self, user_id: UserId) -> str | None:
        """Получить verifier.

        Args:
            user_id: Владелец пароля.
        """
        value: str | None = await self._session.scalar(
            select(CredentialRow.verifier).where(CredentialRow.user_id == user_id)
        )
        return value

    async def set(self, user_id: UserId, verifier: str, now: datetime) -> None:
        """Создать либо заменить парольные данные.

        Args:
            user_id: Владелец пароля.
            verifier: Argon2id verifier.
            now: Время изменения.
        """
        await self._session.execute(
            insert(CredentialRow)
            .values(user_id=user_id, verifier=verifier, changed_at=now)
            .on_conflict_do_update(
                index_elements=[CredentialRow.user_id],
                set_={"verifier": verifier, "changed_at": now},
            )
        )

    async def delete(self, user_id: UserId) -> None:
        """Физически удалить парольные данные аккаунта.

        Args:
            user_id: Владелец данных.
        """
        await self._session.execute(delete(CredentialRow).where(CredentialRow.user_id == user_id))
