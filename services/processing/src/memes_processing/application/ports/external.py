"""Порты объектов и межсервисной доставки."""

from abc import ABC, abstractmethod

from memes_processing.application.dto.coordinator import OutgoingEvent


class IArtifactStorage(ABC):
    """Выдача ограниченного доступа и проверка результата."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Идемпотентно удалить только конкретный объект попытки."""

    @abstractmethod
    async def download_url(self, key: str) -> str:
        """Выдать временное чтение конкретного объекта."""

    @abstractmethod
    async def upload_url(self, key: str) -> str:
        """Выдать временную запись конкретного результата."""

    @abstractmethod
    async def result(self, key: str, *, maximum_bytes: int) -> bytes:
        """Прочитать ограниченный результат для независимой проверки."""


class DeliveryUnavailableError(ConnectionError):
    """Временная недоставка; transport SDK остается внутри Infrastructure."""


class IEventDelivery(ABC):
    """Порт межсервисной REST-доставки."""

    @abstractmethod
    async def deliver(self, event: OutgoingEvent) -> None:
        """Передать идемпотентное сообщение владельцу данных."""


class IPreviewVerifier(ABC):
    """Порт независимой проверки производного артефакта."""

    @abstractmethod
    async def verify(self, data: bytes) -> None:
        """Отклонить поврежденное или несовместимое браузерное превью."""
