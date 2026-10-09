"""Контракты принимаемых проекций и актуального каталога."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from uuid import UUID

from memes_kernel.values import Number, require_aware, require_identifier
from memes_search.application.dto.queries import SearchLimit, SearchMediaKind
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.domain.fragments import MatchedSpan


class FeatureKind(StrEnum):
    """Виды признаков в контракте с координатором обработки."""

    OCR = "ocr"
    TRANSCRIPT = "transcript"
    VISUAL_EMBEDDING = "visual_embedding"
    AUDIO_EMBEDDING = "audio_embedding"
    TEXT_EMBEDDING = "text_embedding"
    PREVIEW = "preview"


class ProjectionState(StrEnum):
    """Результат доставки признака без состояния ML-задачи."""

    PENDING = "pending"
    AVAILABLE = "available"
    EMPTY = "empty"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True, kw_only=True)
class EmbeddingVersion:
    """Полный паспорт вектора и его совместимости."""

    model: str
    weights: str
    task_adapter: str
    preprocessing: str
    space: str
    dimensions: int

    def __post_init__(self) -> None:
        """Потребовать воспроизводимые версии и непустое пространство."""
        if (
            not all(
                item.strip()
                for item in (
                    self.model,
                    self.weights,
                    self.task_adapter,
                    self.preprocessing,
                    self.space,
                )
            )
            or not Number.ONE <= self.dimensions <= SearchLimit.MAXIMUM_VECTOR_DIMENSIONS
        ):
            raise SearchError(SearchErrorCode.INVALID_FEATURE)


@dataclass(frozen=True, slots=True, kw_only=True)
class EmbeddingVector:
    """Вектор совместимого пространства без ML-библиотек."""

    values: tuple[float, ...] = field(repr=False)
    space_id: str
    span: MatchedSpan | None = None
    version: EmbeddingVersion | None = None

    def __post_init__(self) -> None:
        """Запретить пустые, бесконечные и нулевые векторы."""
        if (
            not self.space_id.strip()
            or not self.values
            or len(self.values) > SearchLimit.MAXIMUM_VECTOR_DIMENSIONS
            or not all(isfinite(value) for value in self.values)
            or not any(value != Number.ZERO for value in self.values)
            or (
                self.version is not None
                and (
                    self.version.space != self.space_id
                    or self.version.dimensions != len(self.values)
                )
            )
        ):
            raise SearchError(SearchErrorCode.INVALID_FEATURE)


class FragmentLanguage(StrEnum):
    """Язык распознанного фрагмента."""

    RUSSIAN = "ru"
    ENGLISH = "en"
    UNSPECIFIED = "und"


@dataclass(frozen=True, slots=True, kw_only=True)
class TextFragment:
    """Распознанный текст с интервалом в исходном медиа."""

    text: str = field(repr=False)
    language: FragmentLanguage
    span: MatchedSpan | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureProjection:
    """Версионированное извлечённое содержимое публичного медиа."""

    event_id: UUID
    media_id: UUID
    group_id: UUID
    group_revision: int
    revision: int
    published_at: datetime
    media_kind: SearchMediaKind
    kind: FeatureKind
    cycle_id: UUID
    generation: int
    state: ProjectionState
    version_key: str
    embedding_version: EmbeddingVersion | None = None
    text: str = field(default="", repr=False)
    fragments: tuple[TextFragment, ...] = field(default=(), repr=False)
    vectors: tuple[EmbeddingVector, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        """Проверить ключи событий и монотонные поколения."""
        for identifier in (self.event_id, self.media_id, self.group_id, self.cycle_id):
            require_identifier(identifier)
        require_aware(self.published_at)
        if (
            min(self.revision, self.group_revision, self.generation) < Number.ONE
            or max(len(self.vectors), len(self.fragments)) > SearchLimit.MAXIMUM_FRAGMENTS
        ):
            raise SearchError(SearchErrorCode.INVALID_FEATURE)


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogMedia:
    """Актуальные идентичность и членство из API каталога."""

    media_id: UUID
    group_id: UUID
    group_revision: int
    revision: int
    kind: SearchMediaKind
    published_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaStateChange:
    """Версия публичности медиа для подавления поздних событий."""

    event_id: UUID
    media_id: UUID
    revision: int
    active: bool
    group_id: UUID | None = None
    group_revision: int | None = None
    kind: SearchMediaKind | None = None
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        """Проверить положительную ревизию и полноту активной записи."""
        require_identifier(self.event_id)
        require_identifier(self.media_id)
        if self.revision < Number.ONE:
            raise SearchError(SearchErrorCode.INVALID_FEATURE)
        if self.active and (
            self.group_id is None
            or self.group_revision is None
            or self.kind is None
            or self.published_at is None
        ):
            raise SearchError(SearchErrorCode.INVALID_FEATURE)
