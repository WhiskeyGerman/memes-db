"""Предложения вариаций и пороги модальностей конкретной версии модели."""

from dataclasses import dataclass
from math import isfinite
from uuid import UUID

from memes_kernel.values import Number
from memes_search.application.dto.projections import FeatureKind
from memes_search.application.errors import SearchError, SearchErrorCode


@dataclass(frozen=True, slots=True, kw_only=True)
class VariationMatch:
    """Косинусное совпадение фрагментов с происхождением обоих признаков."""

    media_id: UUID
    score: float
    space_id: str
    model_revision: str
    source_kind: FeatureKind
    target_kind: FeatureKind


@dataclass(frozen=True, slots=True, kw_only=True)
class SimilarityThreshold:
    """Настройка одной пары модальностей совместимой версии весов."""

    space_id: str
    model_revision: str
    source_kind: FeatureKind
    target_kind: FeatureKind
    threshold: float

    def __post_init__(self) -> None:
        """Запретить неподходящие виды признаков и некорректный cosine."""
        embeddings = {
            FeatureKind.VISUAL_EMBEDDING,
            FeatureKind.AUDIO_EMBEDDING,
            FeatureKind.TEXT_EMBEDDING,
        }
        if (
            not self.space_id.strip()
            or not self.model_revision.strip()
            or self.source_kind not in embeddings
            or self.target_kind not in embeddings
            or not isfinite(self.threshold)
            or not Number.ZERO < self.threshold <= Number.ONE
        ):
            raise SearchError(SearchErrorCode.INVALID_FEATURE)

    def matches(self, match: VariationMatch) -> bool:
        """Определить точное соответствие пространства, весов и пары."""
        return (
            self.space_id == match.space_id
            and self.model_revision == match.model_revision
            and self.source_kind is match.source_kind
            and self.target_kind is match.target_kind
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class VariationSuggestion:
    """Актуальная группа-кандидат и лучшее косинусное совпадение."""

    group_id: UUID
    group_revision: int
    media_id: UUID
    score: float
