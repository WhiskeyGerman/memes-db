"""Прикладной порт ISearchIndex."""

from abc import ABC, abstractmethod
from uuid import UUID

from memes_search.application.dto.projections import (
    EmbeddingVector,
    FeatureProjection,
    MediaStateChange,
)
from memes_search.application.dto.queries import SearchQuery
from memes_search.application.dto.samples import SampleFeatures
from memes_search.application.dto.suggestions import VariationMatch
from memes_search.domain.ranking import MediaCandidate


class ISearchIndex(ABC):
    """Контракт адаптера без инфраструктурных зависимостей."""

    @abstractmethod
    async def text(self, query: SearchQuery) -> tuple[tuple[MediaCandidate, ...], ...]:
        """Получить независимые полнотекстовую и нечёткую ветви."""

    @abstractmethod
    async def vectors(
        self, query: SearchQuery, vectors: tuple[EmbeddingVector, ...]
    ) -> tuple[MediaCandidate, ...]:
        """Найти лучшие совпадения фрагментов в совместимых пространствах."""

    @abstractmethod
    async def apply_feature(self, projection: FeatureProjection) -> bool:
        """Применить непросроченное событие единожды."""

    @abstractmethod
    async def apply_media_state(self, change: MediaStateChange) -> bool:
        """Сохранить монотонную версию публичности и членства."""

    @abstractmethod
    async def features_for(self, media_id: UUID) -> SampleFeatures:
        """Получить совместимые признаки активного опубликованного медиа."""

    @abstractmethod
    async def variation_matches(self, media_id: UUID) -> tuple[VariationMatch, ...]:
        """Найти пары фрагментов совместимых весов и преобразований."""
