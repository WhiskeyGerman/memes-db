"""Неизменяемые значения, общие для сервисов."""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from re import fullmatch
from uuid import UUID

from memes_kernel.errors import DomainError


class Number(IntEnum):
    """Границы счётчиков и шаг ревизии."""

    ZERO = 0
    ONE = 1


class CommonViolation(StrEnum):
    """Коды нарушений общих инвариантов."""

    INVALID_REVISION = "invalid_revision"
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_DIGEST = "invalid_digest"
    EMPTY_FILE = "empty_file"
    NAIVE_DATETIME = "naive_datetime"
    TIME_REVERSED = "time_reversed"


class ValuePattern(StrEnum):
    """Форматы проверяемых значений."""

    SHA256 = r"[0-9a-f]{64}"


def require_identifier(value: UUID) -> None:
    """Проверить непустой UUID.

    Args:
        value: Проверяемый идентификатор.

    Raises:
        DomainError: Передан нулевой идентификатор.
    """
    if value.int == Number.ZERO:
        raise DomainError(CommonViolation.INVALID_IDENTIFIER)


def require_aware(value: datetime) -> None:
    """Проверить наличие часового пояса.

    Args:
        value: Проверяемый момент времени.

    Raises:
        DomainError: Момент времени не содержит часового пояса.
    """
    if value.utcoffset() is None:
        raise DomainError(CommonViolation.NAIVE_DATETIME)


def require_ordered(later: datetime, earlier: datetime) -> None:
    """Проверить порядок двух моментов времени.

    Args:
        later: Момент, который должен быть более поздним.
        earlier: Начальный момент.

    Raises:
        DomainError: Часовой пояс отсутствует или время движется назад.
    """
    require_aware(later)
    require_aware(earlier)
    if later < earlier:
        raise DomainError(CommonViolation.TIME_REVERSED)


@dataclass(frozen=True, slots=True, kw_only=True)
class Revision:
    """Положительная ревизия состояния сущности."""

    value: int = Number.ONE

    def __post_init__(self) -> None:
        """Проверить положительность ревизии."""
        if self.value < Number.ONE:
            raise DomainError(CommonViolation.INVALID_REVISION)

    def next(self) -> "Revision":
        """Получить следующую ревизию."""
        return Revision(value=self.value + Number.ONE)


@dataclass(frozen=True, slots=True, kw_only=True)
class Sha256:
    """Нормализованный SHA-256 исходных байтов."""

    value: str

    def __post_init__(self) -> None:
        """Проверить представление хэша."""
        normalized = self.value.lower()
        if fullmatch(ValuePattern.SHA256, normalized) is None:
            raise DomainError(CommonViolation.INVALID_DIGEST)
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True, kw_only=True)
class FileRef:
    """Ссылка на неизменяемое содержимое без адреса хранилища."""

    id: UUID
    sha256: Sha256
    size_bytes: int

    def __post_init__(self) -> None:
        """Проверить идентификатор и размер содержимого."""
        require_identifier(self.id)
        if self.size_bytes < Number.ONE:
            raise DomainError(CommonViolation.EMPTY_FILE)
