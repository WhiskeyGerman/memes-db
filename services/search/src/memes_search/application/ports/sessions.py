"""Прикладной порт ISearchSessionRepository."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_search.application.dto.sessions import SearchSession


class ISearchSessionRepository(ABC):
    """Контракт адаптера без инфраструктурных зависимостей."""

    @abstractmethod
    async def get(self, session_id: UUID) -> SearchSession | None:
        """Получить снимок по идентификатору."""

    @abstractmethod
    async def save(self, session: SearchSession) -> None:
        """Сохранить ограниченный по времени снимок."""

    @abstractmethod
    async def remove_expired(self, now: datetime) -> None:
        """Удалить просроченные приватные запросы."""
