"""Прикладной порт IPrivateStorage."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from uuid import UUID

from memes_kernel.values import FileRef


class IPrivateStorage(ABC):
    """Контракт адаптера без инфраструктурных зависимостей."""

    @abstractmethod
    async def put(
        self, key: str, file_id: UUID, chunks: AsyncIterable[bytes], maximum: int
    ) -> FileRef:
        """Сохранить поток с вычислением SHA-256 и строгим лимитом."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Идемпотентно удалить конкретный объект."""

    @abstractmethod
    async def download_url(self, key: str, lifetime_seconds: int) -> str:
        """Создать временную подписанную ссылку владельцу."""
