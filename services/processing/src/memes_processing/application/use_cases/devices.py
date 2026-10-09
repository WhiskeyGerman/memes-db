"""Регистрация, отзыв и наблюдение доверенных устройств."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID, uuid4

from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    Payload,
    WorkerDevice,
)
from memes_processing.application.dto.features import sequence, string
from memes_processing.application.ports.repository import IProcessingAction, IProcessingRepository
from memes_processing.application.ports.security import IDeviceCredentials
from memes_processing.application.use_cases.jobs import authorize


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterDevice(IProcessingAction):
    """Создать отдельную от пользователей машинную учетную запись."""

    name: str
    credentials: IDeviceCredentials
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Выдать секрет единственный раз без сохранения исходного значения."""
        token, digest = self.credentials.issue()
        worker = WorkerDevice(id=uuid4(), name=self.name, token_hash=digest, created_at=self.now)
        await repository.save_worker(worker)
        return {"worker_id": str(worker.id), "token": token, "created_at": self.now.isoformat()}


@dataclass(frozen=True, slots=True, kw_only=True)
class RevokeDevice(IProcessingAction):
    """Отзыв немедленно блокирует polling, renewal и приемку результата."""

    worker_id: UUID
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Идемпотентно отозвать доверие устройству."""
        worker = await repository.worker(self.worker_id, lock=True)
        if worker is None:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        if worker.revoked_at is None:
            await repository.save_worker(replace(worker, revoked_at=self.now))
        return {"revoked": True}


@dataclass(frozen=True, slots=True, kw_only=True)
class Heartbeat(IProcessingAction):
    """Сообщить реальные ресурсы и разрешенные версии моделей."""

    worker_id: UUID
    token: str = field(repr=False)
    capabilities: Payload
    credentials: IDeviceCredentials
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Обновить наблюдаемость отдельно от продления конкретного lease."""
        worker = await authorize(
            repository, worker_id=self.worker_id, token=self.token, credentials=self.credentials
        )
        await repository.save_worker(
            replace(worker, capabilities=self.capabilities, last_seen_at=self.now)
        )
        requested = tuple(
            UUID(string(item)) for item in sequence(self.capabilities.get("cached_source_ids", []))
        )
        revoked = await repository.revoked_sources(requested, now=self.now)
        return {"accepted": True, "revoked_source_ids": [str(item) for item in revoked]}
