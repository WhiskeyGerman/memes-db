"""Порт транзакционного захвата и подтверждения доставки."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_search.application.dto.delivery import DeliveryMessage


class IOutboxRepository(ABC):
    """Хранилище leases с защитой от позднего подтверждения."""

    @abstractmethod
    async def claim(
        self, now: datetime, lease_until: datetime, limit: int
    ) -> tuple[DeliveryMessage, ...]:
        """Атомарно захватить готовые сообщения и завершить транзакцию."""

    @abstractmethod
    async def acknowledge(self, message_id: UUID, claim_id: UUID) -> None:
        """Удалить сообщение только актуального владельца lease."""

    @abstractmethod
    async def retry(
        self, message_id: UUID, claim_id: UUID, attempts: int, available_at: datetime
    ) -> None:
        """Освободить lease и отложить повтор при актуальном захвате."""
