"""Системное UTC-время для прикладных сценариев."""

from datetime import UTC, datetime

from memes_identity.application.ports.clock import IClock


class SystemClock(IClock):
    """Источник timezone-aware времени операционной системы."""

    def now(self) -> datetime:
        """Получить текущее время UTC."""
        return datetime.now(UTC)
