"""Операторские read models не раскрывают приватные поисковые файлы."""

from dataclasses import dataclass
from uuid import UUID

from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    JSONValue,
    Payload,
)
from memes_processing.application.ports.repository import IProcessingAction, IProcessingRepository


@dataclass(frozen=True, slots=True, kw_only=True)
class ListDevices(IProcessingAction):
    """Показать отозванные и доступные устройства без секретов."""

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Возвращать только эксплуатационные метаданные."""
        workers: list[JSONValue] = [
            {
                "worker_id": str(worker.id),
                "name": worker.name,
                "created_at": worker.created_at.isoformat(),
                "revoked_at": worker.revoked_at.isoformat() if worker.revoked_at else None,
                "last_seen_at": worker.last_seen_at.isoformat() if worker.last_seen_at else None,
                "capabilities": worker.capabilities,
            }
            for worker in await repository.workers()
        ]
        return {"workers": workers}


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectSource(IProcessingAction):
    """Статусы и история циклов доступны оператору отдельно от содержимого."""

    source_id: UUID

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Показать причины сбоев, попытки и fencing состояние без результата."""
        source = await repository.source(self.source_id)
        if source is None:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        tasks: list[JSONValue] = []
        for work in await repository.source_tasks(source.id):
            lease = work.task.lease
            tasks.append(
                {
                    "task_id": str(work.task.id),
                    "cycle_id": str(work.task.input.cycle_id),
                    "kind": work.task.input.kind.value,
                    "generation": work.generation,
                    "state": work.task.state.value,
                    "attempts": work.task.attempts,
                    "failure": work.task.failure.value if work.task.failure else None,
                    "worker_id": str(lease.worker_id) if lease else None,
                    "lease_expires_at": lease.expires_at.isoformat() if lease else None,
                    "deadline": lease.deadline.isoformat() if lease else None,
                }
            )
        return {
            "source_id": str(source.id),
            "source_type": source.type.value,
            "deleted_at": source.deleted_at.isoformat() if source.deleted_at else None,
            "tasks": tasks,
        }
