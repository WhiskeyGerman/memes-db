"""Прикладной порт IMediaInspector."""

from abc import ABC, abstractmethod

from memes_kernel.values import FileRef
from memes_search.application.dto.queries import SearchMediaKind


class IMediaInspector(ABC):
    """Контракт адаптера без инфраструктурных зависимостей."""

    @abstractmethod
    async def inspect(self, key: str, file: FileRef) -> SearchMediaKind:
        """Проверить формат, декодируемость, размеры и длительность."""
