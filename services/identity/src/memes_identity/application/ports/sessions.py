"""Порт конкретного механизма аутентификационных сессий."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_identity.application.dto.authentication import (
    AuthenticatedActor,
    IssuedTokens,
    SessionRotation,
)
from memes_identity.domain.user import UserId


class ISessionService(ABC):
    """Порт выпуска, ротации и отзыва сессий без формата токена в домене."""

    @abstractmethod
    async def issue(self, user_id: UserId, now: datetime) -> IssuedTokens:
        """Открыть сессию.

        Args:
            user_id: Аккаунт владельца.
            now: Серверное время.
        """

    @abstractmethod
    async def authenticate(self, access_token: str, now: datetime) -> AuthenticatedActor | None:
        """Проверить access token и актуальное состояние аккаунта.

        Args:
            access_token: Непрозрачный секрет.
            now: Серверное время.
        """

    @abstractmethod
    async def rotate(self, refresh_token: str, now: datetime) -> SessionRotation:
        """Ротировать refresh token либо отозвать семейство при replay.

        Args:
            refresh_token: Непрозрачный секрет.
            now: Серверное время.
        """

    @abstractmethod
    async def revoke_all(
        self, user_id: UserId, now: datetime, *, except_session_id: UUID | None = None
    ) -> None:
        """Отозвать сессии владельца.

        Args:
            user_id: Аккаунт владельца.
            now: Серверное время.
            except_session_id: Сессия, которую следует сохранить.
        """

    @abstractmethod
    async def revoke(self, session_id: UUID, now: datetime) -> None:
        """Отозвать текущую сессию.

        Args:
            session_id: Идентификатор сессии.
            now: Серверное время.
        """

    @abstractmethod
    async def delete(self, user_id: UserId) -> None:
        """Физически удалить сессии и секреты аккаунта.

        Args:
            user_id: Аккаунт владельца.
        """
