"""Интерфейс исходящего соединения."""

from abc import ABC, abstractmethod
from uuid import UUID

from memes_worker.application.dto.runtime import Payload, WorkAssignment, WorkerFailure, WorkResult


class ICoordinatorClient(ABC):
    """Машина за NAT не предоставляет административный API."""

    @abstractmethod
    async def poll(self, capabilities: Payload) -> WorkAssignment | None:
        """Ожидать совместимую работу."""

    @abstractmethod
    async def revocations(
        self, capabilities: Payload, source_ids: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        """Отозвать локальный кэш уже завершенных удаленных публикаций."""

    @abstractmethod
    async def renew(self, work: WorkAssignment) -> None:
        """Продлить действующий fencing ID."""

    @abstractmethod
    async def submit(self, work: WorkAssignment, result: WorkResult) -> None:
        """Получить durable ACK результата."""

    @abstractmethod
    async def fail(self, work: WorkAssignment, reason: WorkerFailure) -> None:
        """Вернуть безопасную причину сбоя."""
