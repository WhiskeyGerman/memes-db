"""Реальный источник времени web и maintenance процессов."""

from datetime import UTC, datetime

from memes_search.application.ports.clock import IClock


class SystemClock(IClock):
    """Серверное время UTC без локального часового пояса."""

    def now(self) -> datetime:
        """Получить текущий момент в UTC."""
        return datetime.now(UTC)
