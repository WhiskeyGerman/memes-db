"""Пассивная сводка покрытия поискового индекса."""

from dataclasses import dataclass
from datetime import datetime

from memes_search.application.dto.projections import EmbeddingVersion, FeatureKind, ProjectionState


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureCoverage:
    """Количество медиа с одним состоянием и версией признака."""

    kind: FeatureKind
    state: ProjectionState
    version_key: str
    media_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexCoverage:
    """Количество публичных записей без идентификаторов и текстов."""

    active_media: int
    tombstones: int
    vector_media: int
    text_media: int
    features: tuple[FeatureCoverage, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexStatus:
    """Сводка для измеримого сравнения старого и нового deployment."""

    release_id: str
    accepted_embedding_version: EmbeddingVersion
    measured_at: datetime
    coverage: IndexCoverage
