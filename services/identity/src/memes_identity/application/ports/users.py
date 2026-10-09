"""Порт хранения бизнес-учётных записей."""

from abc import ABC, abstractmethod

from memes_identity.domain.user import Login, User, UserId
from memes_kernel.values import Revision


class IUserRepository(ABC):
    """Хранилище аккаунтов с оптимистическим контролем ревизии."""

    @abstractmethod
    async def get(self, user_id: UserId, *, lock: bool = False) -> User | None:
        """Найти аккаунт.

        Args:
            user_id: Идентификатор аккаунта.
            lock: Удержать блокировку до конца транзакции.
        """

    @abstractmethod
    async def batch(self, user_ids: tuple[UserId, ...]) -> tuple[User, ...]:
        """Найти ограниченный набор аккаунтов одной пакетной операцией.

        Args:
            user_ids: Уникальные идентичности.
        """

    @abstractmethod
    async def find_by_login(self, login: Login) -> User | None:
        """Найти аккаунт по нормализованному логину.

        Args:
            login: Нормализованный логин.
        """

    @abstractmethod
    async def save(self, user: User, expected_revision: Revision | None) -> None:
        """Сохранить состояние с проверкой ревизии и уникальности.

        Args:
            user: Сохраняемое состояние.
            expected_revision: Прежняя ревизия; :data:`None` при создании.
        """
