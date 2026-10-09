"""Порт приватных параметров асинхронного запроса."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_search.application.dto.execution import SearchRequest


class ISearchRequestRepository(ABC):
    """Сохранение незавершённых запросов вне web-процесса."""

    @abstractmethod
    async def get(self, request_id: UUID) -> SearchRequest | None:
        """Прочитать ограниченный по времени запрос."""

    @abstractmethod
    async def save(self, request: SearchRequest) -> None:
        """Сохранить связь запроса с задачей и снимком."""

    @abstractmethod
    async def remove_expired(self, now: datetime) -> None:
        """Удалить просроченные параметры inference."""
