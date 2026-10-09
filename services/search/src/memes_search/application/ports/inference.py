"""Прикладной порт IInferenceGateway."""

from abc import ABC, abstractmethod
from uuid import UUID

from memes_search.application.dto.samples import PrivateSample, QueryInference


class IInferenceGateway(ABC):
    """Контракт адаптера без инфраструктурных зависимостей."""

    @abstractmethod
    async def query(
        self, request_id: UUID, text: str, space_id: str, owner_id: UUID | None
    ) -> QueryInference:
        """Запросить реальное вычисление эмбеддинга приватного текста."""

    @abstractmethod
    async def query_result(self, task_id: UUID) -> QueryInference:
        """Прочитать асинхронный результат inference."""

    @abstractmethod
    async def submit_sample(self, sample: PrivateSample) -> None:
        """Поставить проверенный образец на обработку идемпотентно."""

    @abstractmethod
    async def cancel_sample(self, sample_id: UUID) -> None:
        """Отменить ещё не завершённые задачи образца."""

    @abstractmethod
    async def cancel_query(self, task_id: UUID) -> None:
        """Отозвать вычисление и удалить приватный текст пользователя."""
