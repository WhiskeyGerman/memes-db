"""Продуктовый интервал лучшего совпавшего фрагмента."""

from dataclasses import dataclass
from enum import StrEnum

from memes_kernel.errors import DomainError
from memes_kernel.values import Number


class FragmentViolation(StrEnum):
    """Нарушение временного интервала исходного медиа."""

    INVALID_INTERVAL = "invalid_matched_interval"


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchedSpan:
    """Таймкод фрагмента для перехода к месту совпадения."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        """Потребовать неотрицательное начало и положительную длительность."""
        if self.start_ms < Number.ZERO or self.end_ms <= self.start_ms:
            raise DomainError(FragmentViolation.INVALID_INTERVAL)
