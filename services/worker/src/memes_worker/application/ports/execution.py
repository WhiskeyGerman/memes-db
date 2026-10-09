"""Интерфейсы исполнения и удаляемого кэша."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_worker.application.dto.runtime import WorkAssignment, WorkResult
from memes_worker.application.execution import ExecutionKey


class ITaskExecutor(ABC):
    """Известные операции исполняются в ограниченном процессе."""

    @abstractmethod
    async def execute(self, work: WorkAssignment) -> WorkResult:
        """Вычислить реальный результат."""


class IResultCache(ABC):
    """Кэш не определяет серверную идемпотентность."""

    @abstractmethod
    async def get(self, key: ExecutionKey, *, now: datetime) -> WorkResult | None:
        """Вернуть содержимое без продления TTL."""

    @abstractmethod
    async def put(self, result: WorkResult, *, expires_at: datetime) -> None:
        """Сохранить результат с неизменяемым TTL."""

    @abstractmethod
    async def purge(self, *, now: datetime) -> None:
        """Удалить просроченное, включая старт."""

    @abstractmethod
    async def revoke(self, source_id: UUID) -> None:
        """Удалить результат отмененного источника."""

    @abstractmethod
    async def source_ids(self) -> tuple[UUID, ...]:
        """Получить ограниченный набор источников текущего кэша."""

    @abstractmethod
    async def full(self) -> bool:
        """Проверить общий дисковый предел."""
