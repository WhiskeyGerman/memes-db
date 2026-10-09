"""Данные долговечной доставки и политика повторов."""

from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum, StrEnum
from uuid import UUID

from memes_kernel.values import Number
from memes_search.application.dto.samples import PrivateSample


class OutboxAction(StrEnum):
    """Действия приватного обработчика."""

    SUBMIT = "submit"
    CANCEL = "cancel"
    CANCEL_QUERY = "cancel_query"
    DELETE_OBJECT = "delete_object"


class RetryLimit(IntEnum):
    """Ограничение экспоненты при неограниченной надёжной доставке."""

    BASE = 2
    EXPONENT = 16


class DeliveryLimit(IntEnum):
    """Запас lease для SQL ACK/retry после сетевого прохода."""

    PASS_LEASE_DIVISOR = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryMessage:
    """Захваченное сообщение с lease, независимым от HTTP."""

    id: UUID
    claim_id: UUID
    action: OutboxAction
    attempts: int
    sample: PrivateSample | None = None
    sample_id: UUID | None = None
    task_id: UUID | None = None
    object_key: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryPolicy:
    """Конфигурируемые ограничения фонового процесса."""

    batch: int
    lease: timedelta
    initial_retry: timedelta
    maximum_retry: timedelta

    def retry_delay(self, attempts: int) -> timedelta:
        """Рассчитать ограниченную экспоненциальную задержку."""
        multiplier = Number.ONE << min(max(attempts - Number.ONE, Number.ZERO), RetryLimit.EXPONENT)
        return min(self.initial_retry * multiplier, self.maximum_retry)


@dataclass(frozen=True, slots=True, kw_only=True)
class MaintenanceResult:
    """Счётчики одного ограниченного прохода."""

    completed: int
    failed: int
    continue_immediately: bool = False
