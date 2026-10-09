"""Порт общесистемной квоты приватных образцов двух Search индексов."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID


class ISampleAdmission(ABC):
    """Единый admission ресурс; отказ происходит до чтения upload body."""

    @abstractmethod
    async def reserve(
        self, *, sample_id: UUID, owner_id: UUID, created_at: datetime, expires_at: datetime
    ) -> None:
        """Атомарно зарезервировать ограниченный слот владельца.

        Args:
            sample_id: Устойчивый ID новой передачи.
            owner_id: Авторизованный владелец.
            created_at: Время создания приватного образца.
            expires_at: Ограниченный срок образца.
        """

    @abstractmethod
    async def release(self, sample_id: UUID) -> None:
        """Освободить слот после READY, FAILED или CANCELLED.

        Args:
            sample_id: Идемпотентная идентичность резерва.
        """

    @abstractmethod
    async def erase_owner(self, owner_id: UUID) -> None:
        """Установить постоянный owner fence и снять все его резервы.

        Args:
            owner_id: Удалённая авторизованная идентичность.
        """

    @abstractmethod
    async def purge(self, now: datetime) -> None:
        """Удалить истёкшие резервы в процессе владельца ledger.

        Args:
            now: Серверное время maintenance.
        """
