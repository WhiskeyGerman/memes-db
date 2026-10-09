"""Единица PostgreSQL-транзакции сервиса Identity."""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.ports.outbox import IAccountOutbox
from memes_identity.application.ports.passwords import ICredentialRepository
from memes_identity.application.ports.rate_limit import ILoginRateLimiter
from memes_identity.application.ports.sessions import ISessionService
from memes_identity.application.ports.unit_of_work import IIdentityUnitOfWork
from memes_identity.application.ports.users import IUserRepository
from memes_identity.infrastructure.authentication.sessions import SqlSessionService
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.credentials import SqlCredentialRepository
from memes_identity.infrastructure.persistence.outbox import SqlAccountOutbox
from memes_identity.infrastructure.persistence.rate_limit import SqlLoginRateLimiter
from memes_identity.infrastructure.persistence.users import SqlUserRepository


class SqlIdentityUnitOfWork(IIdentityUnitOfWork):
    """Изолированная транзакция; незакоммиченное всегда откатывается."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], settings: IdentitySettings
    ) -> None:
        """Получить фабрику сессий и параметры адаптеров.

        Args:
            factory: Фабрика PostgreSQL-сессий.
            settings: Конфигурация релиза.
        """
        self._factory = factory
        self._settings = settings
        self._session: AsyncSession | None = None

    @property
    def users(self) -> IUserRepository:
        """Получить репозиторий аккаунтов."""
        return SqlUserRepository(self._required_session())

    @property
    def credentials(self) -> ICredentialRepository:
        """Получить репозиторий verifier."""
        return SqlCredentialRepository(self._required_session())

    @property
    def sessions(self) -> ISessionService:
        """Получить механизм сессий."""
        return SqlSessionService(self._required_session(), self._settings)

    @property
    def rate_limiter(self) -> ILoginRateLimiter:
        """Получить распределённый ограничитель."""
        return SqlLoginRateLimiter(self._required_session(), self._settings)

    @property
    def outbox(self) -> IAccountOutbox:
        """Получить transactional outbox."""
        return SqlAccountOutbox(self._required_session(), self._settings)

    async def __aenter__(self) -> Self:
        """Открыть новую сессию для текущего сценария."""
        if self._session is not None:
            raise RuntimeError("identity_transaction_already_open")
        self._session = self._factory()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Откатить незавершённое и освободить соединение.

        Args:
            exc_type: Тип ошибки.
            exc_value: Ошибка.
            traceback: Стек ошибки.
        """
        session = self._required_session()
        try:
            await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        """Зафиксировать текущие изменения."""
        await self._required_session().commit()

    def _required_session(self) -> AsyncSession:
        """Получить открытую транзакцию.

        Returns:
            AsyncSession: Текущая сессия.

        Raises:
            RuntimeError: Сценарий вызван вне транзакции.
        """
        if self._session is None:
            raise RuntimeError("identity_transaction_not_open")
        return self._session
