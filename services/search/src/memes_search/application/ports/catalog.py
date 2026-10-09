"""Прикладной порт ICatalogReader."""

from abc import ABC, abstractmethod
from uuid import UUID

from memes_kernel.values import Sha256
from memes_search.application.dto.projections import CatalogMedia


class ICatalogReader(ABC):
    """Контракт адаптера без инфраструктурных зависимостей."""

    @abstractmethod
    async def resolve(self, media_ids: tuple[UUID, ...]) -> tuple[CatalogMedia, ...]:
        """Получить только активные медиа с актуальным членством."""

    @abstractmethod
    async def by_hash(self, digest: Sha256) -> CatalogMedia | None:
        """Найти точное совпадение только среди активных медиа."""
