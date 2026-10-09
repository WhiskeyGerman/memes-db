"""Порт серверного времени."""

from abc import ABC, abstractmethod
from datetime import datetime


class IClock(ABC):
    """Источник timezone-aware серверного времени."""

    @abstractmethod
    def now(self) -> datetime:
        """Получить текущее время UTC."""
