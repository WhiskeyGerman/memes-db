"""Порт атомарной прикладной транзакции Identity."""

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self

from memes_identity.application.ports.outbox import IAccountOutbox
from memes_identity.application.ports.passwords import ICredentialRepository
from memes_identity.application.ports.rate_limit import ILoginRateLimiter
from memes_identity.application.ports.sessions import ISessionService
from memes_identity.application.ports.users import IUserRepository


class IIdentityUnitOfWork(ABC):
    """Атомарное состояние пользователя, секретов, сессий и outbox."""

    @property
    @abstractmethod
    def users(self) -> IUserRepository:
        """Получить хранилище аккаунтов."""

    @property
    @abstractmethod
    def credentials(self) -> ICredentialRepository:
        """Получить хранилище verifier."""

    @property
    @abstractmethod
    def sessions(self) -> ISessionService:
        """Получить механизм сессий в текущей транзакции."""

    @property
    @abstractmethod
    def rate_limiter(self) -> ILoginRateLimiter:
        """Получить распределённый ограничитель входа."""

    @property
    @abstractmethod
    def outbox(self) -> IAccountOutbox:
        """Получить transactional outbox."""

    @abstractmethod
    async def __aenter__(self) -> Self:
        """Открыть прикладную транзакцию."""

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Закрыть транзакцию и откатить незавершённые изменения.

        Args:
            exc_type: Тип ошибки.
            exc_value: Ошибка.
            traceback: Стек ошибки.
        """

    @abstractmethod
    async def commit(self) -> None:
        """Зафиксировать изменения текущей транзакции."""
