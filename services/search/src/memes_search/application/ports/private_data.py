"""Порт атомарного удаления и fencing приватных данных Search."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID


class IPrivateDataRepository(ABC):
    """Единая транзакционная граница запрета поздних private записей."""

    @abstractmethod
    async def erase_owner(self, owner_id: UUID, now: datetime) -> None:
        """Зафиксировать fence, стереть запросы и поставить надёжные отмены."""
