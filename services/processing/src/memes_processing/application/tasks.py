"""Прикладная диспетчеризация попыток; очередь и lease не входят в домен."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from math import isfinite
from uuid import UUID

from memes_kernel.values import FileRef, Number, require_aware, require_identifier, require_ordered
from memes_processing.domain.features import CycleId, ExtractionId, FeatureKind, FeatureVersion


class TaskState(StrEnum):
    """Состояние работы координатора."""

    WAITING = "waiting"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskFailure(StrEnum):
    """Категории сбоя без секретов и трассировок."""

    TEMPORARY = "temporary"
    INVALID_INPUT = "invalid_input"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    LEASE_EXPIRED = "lease_expired"


class SchedulingViolation(StrEnum):
    """Причины отказа координатора принять переход."""

    INVALID_POLICY = "invalid_retry_policy"
    INVALID_STATE = "invalid_task_state"
    STALE_ATTEMPT = "stale_task_attempt"
    EXPIRED_LEASE = "expired_task_lease"
    INVALID_RETRY = "invalid_retry_number"


class SchedulingError(ValueError):
    """Ошибка прикладной диспетчеризации."""

    def __init__(self, code: SchedulingViolation) -> None:
        """Сохранить машинную причину отказа.

        Args:
            code: Причина отказа.
        """
        self.code = code
        super().__init__(code.value)


class TaskLimit(IntEnum):
    """Начальные параметры координатора."""

    RETRIES = 3
    BASE_DELAY_SECONDS = 5
    BACKOFF_FACTOR = 2
    MAX_DELAY_SECONDS = 300
    JITTER_PERCENT = 20
    PERCENT_SCALE = 100
    LEASE_SECONDS = 120
    TASK_SECONDS = 600


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """Конфигурация повторов; случайность передаётся явно для проверки."""

    maximum_retries: int = TaskLimit.RETRIES
    base_delay: timedelta = timedelta(seconds=TaskLimit.BASE_DELAY_SECONDS)
    factor: int = TaskLimit.BACKOFF_FACTOR
    maximum_delay: timedelta = timedelta(seconds=TaskLimit.MAX_DELAY_SECONDS)
    jitter_fraction: float = TaskLimit.JITTER_PERCENT / TaskLimit.PERCENT_SCALE

    def __post_init__(self) -> None:
        """Проверить пределы и коэффициенты."""
        if (
            self.maximum_retries < Number.ZERO
            or self.base_delay <= timedelta()
            or self.maximum_delay < self.base_delay
            or self.factor < Number.ONE
            or not isfinite(self.jitter_fraction)
            or not Number.ZERO <= self.jitter_fraction <= Number.ONE
        ):
            raise SchedulingError(SchedulingViolation.INVALID_POLICY)

    def delay(self, retry_number: int, *, random_fraction: float) -> timedelta:
        """Вычислить экспоненциальную задержку очередного повтора.

        Args:
            retry_number: Номер повтора начиная с единицы.
            random_fraction: Случайное число от нуля до единицы включительно.

        Returns:
            timedelta: Ограниченная задержка с добавленным jitter.
        """
        if (
            not Number.ONE <= retry_number <= self.maximum_retries
            or not isfinite(random_fraction)
            or not Number.ZERO <= random_fraction <= Number.ONE
        ):
            raise SchedulingError(SchedulingViolation.INVALID_RETRY)
        delay = self.base_delay
        for _ in range(retry_number - Number.ONE):
            delay = min(self.maximum_delay, delay * self.factor)
            if delay == self.maximum_delay:
                break
        return delay * (Number.ONE + self.jitter_fraction * random_fraction)


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskInput:
    """Контракт задания без ссылок на SDK и транспорт брокера."""

    extraction_id: ExtractionId
    cycle_id: CycleId
    source: FileRef
    kind: FeatureKind
    version: FeatureVersion


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptLease:
    """Одно назначение доверенному исполнителю с уникальным fencing ID."""

    id: UUID
    worker_id: UUID
    started_at: datetime
    expires_at: datetime
    deadline: datetime

    def __post_init__(self) -> None:
        """Проверить идентификаторы и временное окно."""
        require_identifier(self.id)
        require_identifier(self.worker_id)
        require_ordered(self.expires_at, self.started_at)
        require_ordered(self.deadline, self.expires_at)
        if self.expires_at == self.started_at:
            raise SchedulingError(SchedulingViolation.INVALID_STATE)

    def require_current(self, attempt_id: UUID, *, now: datetime) -> None:
        """Запретить результат старой или просроченной попытки.

        Args:
            attempt_id: Идентификатор отправителя результата.
            now: Серверное время проверки.
        """
        require_ordered(now, self.started_at)
        if self.id != attempt_id:
            raise SchedulingError(SchedulingViolation.STALE_ATTEMPT)
        if now >= min(self.expires_at, self.deadline):
            raise SchedulingError(SchedulingViolation.EXPIRED_LEASE)

    def renew(self, *, attempt_id: UUID, now: datetime, duration: timedelta) -> "AttemptLease":
        """Продлить текущее назначение до предела всей попытки.

        Args:
            attempt_id: Продлеваемая попытка.
            now: Серверное время.
            duration: Новый срок lease от текущего времени.
        """
        self.require_current(attempt_id, now=now)
        if duration <= timedelta():
            raise SchedulingError(SchedulingViolation.INVALID_POLICY)
        return replace(self, expires_at=min(self.deadline, max(self.expires_at, now + duration)))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessingTask:
    """Версионированное состояние задания для атомарного сохранения."""

    id: UUID
    input: TaskInput
    created_at: datetime
    state: TaskState = TaskState.WAITING
    attempts: int = Number.ZERO
    lease: AttemptLease | None = None
    ready_at: datetime | None = None
    failure: TaskFailure | None = None

    def __post_init__(self) -> None:
        """Запретить неоднозначное состояние диспетчеризации."""
        require_identifier(self.id)
        require_aware(self.created_at)
        if (
            self.attempts < Number.ZERO
            or (
                self.state in {TaskState.RUNNING, TaskState.SUCCEEDED, TaskState.FAILED}
                and self.attempts < Number.ONE
            )
            or (self.state is TaskState.RUNNING) != (self.lease is not None)
            or (self.state is TaskState.RETRY_WAIT) != (self.ready_at is not None)
            or (self.state in {TaskState.FAILED, TaskState.RETRY_WAIT})
            != (self.failure is not None)
        ):
            raise SchedulingError(SchedulingViolation.INVALID_STATE)
        if self.lease is not None:
            require_ordered(self.lease.started_at, self.created_at)
        if self.ready_at is not None:
            require_ordered(self.ready_at, self.created_at)

    def assign(self, *, lease: AttemptLease, now: datetime) -> "ProcessingTask":
        """Начать попытку только после фактического назначения воркеру.

        Args:
            lease: Новое назначение.
            now: Серверное время.
        """
        require_ordered(now, self.created_at)
        if self.state not in {TaskState.WAITING, TaskState.RETRY_WAIT}:
            raise SchedulingError(SchedulingViolation.INVALID_STATE)
        if self.ready_at is not None and now < self.ready_at:
            raise SchedulingError(SchedulingViolation.INVALID_STATE)
        lease.require_current(lease.id, now=now)
        return replace(
            self,
            state=TaskState.RUNNING,
            attempts=self.attempts + Number.ONE,
            lease=lease,
            ready_at=None,
            failure=None,
        )

    def accept(self, *, attempt_id: UUID, now: datetime) -> "ProcessingTask":
        """Завершить действующую попытку; содержимое проверяется доменом.

        Args:
            attempt_id: Попытка полученного результата.
            now: Серверное время.
        """
        if self.state is not TaskState.RUNNING or self.lease is None:
            raise SchedulingError(SchedulingViolation.INVALID_STATE)
        self.lease.require_current(attempt_id, now=now)
        return replace(self, state=TaskState.SUCCEEDED, lease=None)

    def fail(
        self,
        *,
        attempt_id: UUID,
        reason: TaskFailure,
        policy: RetryPolicy,
        random_fraction: float,
        now: datetime,
    ) -> "ProcessingTask":
        """Зафиксировать сбой или истечение lease с конечным числом повторов.

        Args:
            attempt_id: Неудачная попытка.
            reason: Категория сбоя.
            policy: Политика текущего задания.
            random_fraction: Источник jitter.
            now: Серверное время.
        """
        if self.state is not TaskState.RUNNING or self.lease is None:
            raise SchedulingError(SchedulingViolation.INVALID_STATE)
        require_ordered(now, self.lease.started_at)
        if self.lease.id != attempt_id:
            raise SchedulingError(SchedulingViolation.STALE_ATTEMPT)
        if reason is TaskFailure.LEASE_EXPIRED:
            if now < min(self.lease.expires_at, self.lease.deadline):
                raise SchedulingError(SchedulingViolation.INVALID_STATE)
        else:
            self.lease.require_current(attempt_id, now=now)
        if reason is TaskFailure.INVALID_INPUT or self.attempts > policy.maximum_retries:
            return replace(self, state=TaskState.FAILED, lease=None, failure=reason)
        delay = policy.delay(self.attempts, random_fraction=random_fraction)
        return replace(
            self,
            state=TaskState.RETRY_WAIT,
            lease=None,
            failure=reason,
            ready_at=now + delay,
        )

    def cancel(self) -> "ProcessingTask":
        """Отменить работу после удаления источника или истечения образца."""
        if self.state in {
            TaskState.CANCELLED,
            TaskState.SUCCEEDED,
            TaskState.EMPTY,
            TaskState.FAILED,
        }:
            return self
        return replace(self, state=TaskState.CANCELLED, lease=None, ready_at=None, failure=None)
