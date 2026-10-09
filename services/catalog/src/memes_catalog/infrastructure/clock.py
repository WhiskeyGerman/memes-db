"""Часы UTC для длительных операций приёмки."""

from datetime import UTC, datetime

from memes_catalog.application.ports.clock import IClock


class SystemClock(IClock):
    """Реальный источник времени."""

    def now(self) -> datetime:
        """Получить aware UTC timestamp."""
        return datetime.now(UTC)
