"""Прикладной порт ISampleRepository."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_search.application.dto.samples import PrivateSample, SampleFeatureChange, SampleFeatures


class ISampleRepository(ABC):
    """Контракт адаптера без инфраструктурных зависимостей."""

    @abstractmethod
    async def reserve(self, sample: PrivateSample, limit: int) -> None:
        """Атомарно зарезервировать место в квоте владельца."""

    @abstractmethod
    async def get(self, sample_id: UUID) -> PrivateSample | None:
        """Прочитать состояние образца."""

    @abstractmethod
    async def save(self, sample: PrivateSample, *, enqueue: bool = False) -> None:
        """Сохранить состояние и атомарно создать доставку координатору."""

    @abstractmethod
    async def features(self, sample_id: UUID) -> SampleFeatures:
        """Прочитать готовые признаки владельца."""

    @abstractmethod
    async def apply_features(self, change: SampleFeatureChange, now: datetime) -> bool:
        """Принять результат только актуального образца."""

    @abstractmethod
    async def expired(self, now: datetime, limit: int) -> tuple[PrivateSample, ...]:
        """Получить ограниченную порцию файлов для очистки."""

    @abstractmethod
    async def remove(self, sample_id: UUID) -> None:
        """Удалить очищенное состояние и результаты."""

    @abstractmethod
    async def schedule_cleanup(self, object_key: str, available_at: datetime) -> None:
        """Сохранить долговечное удаление позднего S3-файла."""

    @abstractmethod
    async def schedule_cancel(self, sample_id: UUID, available_at: datetime) -> None:
        """Сохранить отмену обработки независимо от удаляемого образца."""
