"""Порт внешнего журнала авторизованных намерений удаления."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from memes_platform.application.dto.json import JsonObject


class IDeletionJournal(ABC):
    """Защита удаления при восстановлении PostgreSQL из старой копии."""

    @abstractmethod
    async def record(self, event: JsonObject) -> None:
        """Подтвердить durable запись намерения до ответа пользователю.

        Args:
            event: Обезличенное событие без секретов и содержимого.
        """

    @abstractmethod
    def records(self, *, since: datetime | None = None) -> AsyncIterator[JsonObject]:
        """Прочитать журнал для административного восстановления.

        Args:
            since: Нижняя граница времени записи S3.
        """
