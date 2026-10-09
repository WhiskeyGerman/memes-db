"""Порты безопасного хранения и проверки паролей."""

from abc import ABC, abstractmethod
from datetime import datetime

from memes_identity.domain.user import UserId


class IPasswordVerifier(ABC):
    """Вычисление и проверка непрозрачного парольного verifier."""

    @abstractmethod
    async def encode(self, password: str) -> str:
        """Закодировать исходный пароль.

        Args:
            password: Пароль без преобразований.
        """

    @abstractmethod
    async def verify(self, password: str, verifier: str | None) -> bool:
        """Проверить пароль, включая фиктивную проверку неизвестного аккаунта.

        Args:
            password: Исходный пароль.
            verifier: Сохранённое представление либо :data:`None`.
        """


class ICredentialRepository(ABC):
    """Хранилище парольных данных, не являющихся бизнес-сущностями."""

    @abstractmethod
    async def get(self, user_id: UserId) -> str | None:
        """Получить парольный verifier.

        Args:
            user_id: Владелец данных.
        """

    @abstractmethod
    async def set(self, user_id: UserId, verifier: str, now: datetime) -> None:
        """Создать либо заменить парольные данные.

        Args:
            user_id: Владелец данных.
            verifier: Безопасное представление пароля.
            now: Время изменения.
        """

    @abstractmethod
    async def delete(self, user_id: UserId) -> None:
        """Удалить парольные данные.

        Args:
            user_id: Владелец данных.
        """
