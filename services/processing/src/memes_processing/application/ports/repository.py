"""Интерфейсы транзакции и хранилища координатора."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_processing.application.dto.coordinator import (
    CoordinatorLimit,
    OutgoingEvent,
    Payload,
    Source,
    WorkerDevice,
    WorkItem,
)


class IProcessingRepository(ABC):
    """Порт атомарной транзакции и блокировки заданий."""

    @abstractmethod
    async def status(self, *, now: datetime, lease_seconds: int) -> Payload:
        """Прочитать нагрузку очередей и доступность устройств."""

    @abstractmethod
    async def schedule_cleanup(self, key: str, *, ready_at: datetime) -> None:
        """Сохранить удаление артефакта до выдачи ссылки на запись."""

    @abstractmethod
    async def retain_artifact(self, key: str) -> None:
        """Принятое превью публикации принадлежит жизненному циклу каталога."""

    @abstractmethod
    async def cleanup(
        self, *, now: datetime, limit: int = CoordinatorLimit.OUTBOX_BATCH
    ) -> list[str]:
        """Заблокировать партию артефактов с истекшими signed URLs."""

    @abstractmethod
    async def cleanup_done(self, key: str) -> None:
        """Подтвердить удаление конкретного объекта."""

    @abstractmethod
    async def claim_event(self, event_id: UUID) -> bool:
        """Зарегистрировать входящее сообщение ровно один раз."""

    @abstractmethod
    async def lock_source(self, source_id: UUID) -> None:
        """Сериализовать публикацию и отмену даже до появления строки."""

    @abstractmethod
    async def source_deleted(self, source_id: UUID) -> bool:
        """Проверить tombstone даже при отсутствии оригинала."""

    @abstractmethod
    async def mark_deleted(self, source_id: UUID, *, now: datetime) -> None:
        """Зафиксировать tombstone и очистку приватных событий и артефактов."""

    @abstractmethod
    async def source(self, source_id: UUID, *, lock: bool = False) -> Source | None:
        """Получить источник, при необходимости удерживая блокировку."""

    @abstractmethod
    async def save_source(self, source: Source) -> None:
        """Сохранить новое состояние оригинала."""

    @abstractmethod
    async def work(self, task_id: UUID, *, lock: bool = False) -> WorkItem | None:
        """Получить задание с источником."""

    @abstractmethod
    async def save_work(self, work: WorkItem) -> None:
        """Сохранить задачу и независимый результат."""

    @abstractmethod
    async def source_tasks(self, source_id: UUID) -> list[WorkItem]:
        """Загрузить все этапы оригинала."""

    @abstractmethod
    async def available(
        self, *, worker_id: UUID, capabilities: Payload, now: datetime
    ) -> WorkItem | None:
        """Атомарно выбрать совместимую задачу через :sql:`SKIP LOCKED`."""

    @abstractmethod
    async def expired_sources(self, *, now: datetime) -> list[Source]:
        """Заблокировать приватные источники, срок которых уже истек."""

    @abstractmethod
    async def expired(self, *, now: datetime) -> list[WorkItem]:
        """Заблокировать ограниченную партию истекших назначений."""

    @abstractmethod
    async def revoked_sources(self, source_ids: tuple[UUID, ...], *, now: datetime) -> list[UUID]:
        """Отозвать кэш неизвестных, удаленных и истекших источников."""

    @abstractmethod
    async def workers(self) -> list[WorkerDevice]:
        """Получить ограниченную партию эксплуатационных метаданных устройств."""

    @abstractmethod
    async def worker(self, worker_id: UUID, *, lock: bool = False) -> WorkerDevice | None:
        """Получить доверенное устройство."""

    @abstractmethod
    async def save_worker(self, worker: WorkerDevice) -> None:
        """Сохранить регистрацию или отзыв устройства."""

    @abstractmethod
    async def attempt(
        self, work: WorkItem, *, state: str, finished_at: datetime | None = None
    ) -> None:
        """Сохранить историю текущего назначения."""

    @abstractmethod
    async def enqueue(self, event: OutgoingEvent) -> None:
        """Сохранить исходящий REST-вызов в текущей транзакции."""

    @abstractmethod
    async def outgoing(
        self, *, now: datetime, limit: int = CoordinatorLimit.OUTBOX_BATCH
    ) -> list[OutgoingEvent]:
        """Заблокировать упорядоченную партию недоставленных сообщений."""

    @abstractmethod
    async def acknowledge(self, event_id: UUID) -> None:
        """Подтвердить успешную доставку."""

    @abstractmethod
    async def postpone(self, event: OutgoingEvent, *, now: datetime) -> None:
        """Зафиксировать задержку следующей попытки доставки."""


class ITransaction(ABC):
    """Создание транзакции принадлежит инфраструктурному адаптеру."""

    @abstractmethod
    async def run(self, action: "IProcessingAction") -> Payload:
        """Исполнить прикладное действие в транзакции."""


class IProcessingAction(ABC):
    """Прикладная операция с единым атомарным контекстом."""

    @abstractmethod
    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Выполнить действие внутри транзакции."""
