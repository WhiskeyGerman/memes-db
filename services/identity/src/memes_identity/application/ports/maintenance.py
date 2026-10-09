"""Порт очистки завершённых технических записей Identity."""

from abc import ABC, abstractmethod
from datetime import datetime


class IIdentityMaintenance(ABC):
    """Атомарное удаление только истёкших технических данных."""

    @abstractmethod
    async def purge(
        self, *, sessions_before: datetime, rates_before: datetime, deliveries_before: datetime
    ) -> None:
        """Очистить записи согласно уже рассчитанным прикладным срокам.

        Args:
            sessions_before: Верхняя граница срока жизни сессии.
            rates_before: Верхняя граница старого окна входа.
            deliveries_before: Верхняя граница завершённой доставки.
        """
