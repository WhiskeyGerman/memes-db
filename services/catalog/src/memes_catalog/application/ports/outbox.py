"""Порт надёжной доставки сохранённых событий."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_catalog.application.dto.catalog import OutgoingEvent


class IOutboxRepository(ABC):
    """Хранение прав владения REST-доставкой."""

    @abstractmethod
    async def acquire(self, *, now: datetime, limit: int) -> tuple[OutgoingEvent, ...]:
        """Получить ограниченную порцию с lease.

        Args:
            now: Серверное время.
            limit: Размер порции.
        """

    @abstractmethod
    async def acknowledge(self, event_id: UUID, claim_id: UUID) -> None:
        """Подтвердить доставку только владельцем.

        Args:
            event_id: Событие.
            claim_id: Право владения.
        """

    @abstractmethod
    async def retry(self, event: OutgoingEvent, *, now: datetime) -> None:
        """Вернуть событие в очередь с backoff.

        Args:
            event: Выданное событие.
            now: Серверное время.
        """
