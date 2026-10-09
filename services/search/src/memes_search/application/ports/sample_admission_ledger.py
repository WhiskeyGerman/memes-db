"""Порт атомарного состояния квоты без вычисления серверной TTL policy."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID


class ISampleAdmissionLedger(ABC):
    """Общее состояние reservations и permanent owner fence."""

    @abstractmethod
    async def reserve(
        self,
        *,
        sample_id: UUID,
        owner_id: UUID,
        created_at: datetime,
        expires_at: datetime,
        now: datetime,
        limit: int,
    ) -> None:
        """Проверить fence и квоту в одной транзакции под owner lock.

        Args:
            sample_id: ID передачи.
            owner_id: Владелец.
            created_at: Проверенное время создания.
            expires_at: Проверенный конечный срок.
            now: Серверное время.
            limit: Серверный предел из Application policy.
        """

    @abstractmethod
    async def release(self, sample_id: UUID) -> None:
        """Удалить только выбранный технический резерв.

        Args:
            sample_id: ID передачи.
        """

    @abstractmethod
    async def erase_owner(self, owner_id: UUID, now: datetime) -> None:
        """Атомарно запретить новые резервы и удалить прежние.

        Args:
            owner_id: Удалённый владелец.
            now: Время постоянного fence.
        """

    @abstractmethod
    async def purge(self, now: datetime) -> None:
        """Очистить истёкшее техническое состояние.

        Args:
            now: Серверное время.
        """
