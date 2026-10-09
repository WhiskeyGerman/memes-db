"""Порт серверного времени для длительных сценариев."""

from abc import ABC, abstractmethod
from datetime import datetime


class IClock(ABC):
    """Источник aware времени без привязки к ОС."""

    @abstractmethod
    def now(self) -> datetime:
        """Получить текущее серверное время."""
