"""Прикладной порт IClock."""

from abc import ABC, abstractmethod
from datetime import datetime


class IClock(ABC):
    """Контракт адаптера без инфраструктурных зависимостей."""

    @abstractmethod
    def now(self) -> datetime:
        """Получить серверное время с часовым поясом."""
