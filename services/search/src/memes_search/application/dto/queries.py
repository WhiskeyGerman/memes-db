"""Параметры запроса и независимые поисковые фильтры."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from memes_kernel.values import FileRef, Number, require_aware, require_identifier
from memes_search.application.errors import SearchError, SearchErrorCode


class SearchMode(StrEnum):
    """Способы получения кандидатов."""

    TEXT = "text"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
    SAMPLE = "sample"


class SearchLimit(IntEnum):
    """Предельные значения из продуктовых требований."""

    QUERY_CHARACTERS = 1_000
    DEFAULT_PAGE_SIZE = 20
    MAXIMUM_PAGE_SIZE = 100
    SESSION_SECONDS = 600
    MAXIMUM_SAMPLE_BYTES = 104_857_600
    MAXIMUM_SAMPLE_SECONDS = 300
    ACTIVE_SAMPLES_PER_USER = 2
    CHUNK_BYTES = 1_048_576
    CLEANUP_SECONDS = 30
    EMBEDDING_INDEX_DIMENSIONS = 768
    MAXIMUM_VECTOR_DIMENSIONS = 16_384
    MAXIMUM_FRAGMENTS = 18_000
    VARIATION_SUGGESTIONS = 10
    CATALOG_BATCH_SIZE = 1000
    DOWNLOAD_SECONDS = 60


class SearchMediaKind(StrEnum):
    """Публичные типы медиа, известные поисковому контексту."""

    IMAGE = "image"
    ANIMATION = "animation"
    VIDEO = "video"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchFilters:
    """Ограничение по виду медиа и моменту публикации."""

    kinds: tuple[SearchMediaKind, ...] = ()
    published_after: datetime | None = None
    published_before: datetime | None = None

    def __post_init__(self) -> None:
        """Проверить часовой пояс и порядок временных фильтров."""
        for value in (self.published_after, self.published_before):
            if value is not None:
                require_aware(value)
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after >= self.published_before
        ):
            raise SearchError(SearchErrorCode.INVALID_QUERY)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchQuery:
    """Запрос без транспортных схем и поисковых DSL."""

    mode: SearchMode = SearchMode.HYBRID
    text: str | None = field(default=None, repr=False)
    sample: FileRef | None = None
    sample_id: UUID | None = None
    owner_id: UUID | None = None
    page_size: int = SearchLimit.DEFAULT_PAGE_SIZE
    filters: SearchFilters = field(default_factory=SearchFilters)

    def __post_init__(self) -> None:
        """Проверить вход выбранного режима."""
        if not Number.ONE <= self.page_size <= SearchLimit.MAXIMUM_PAGE_SIZE:
            raise SearchError(SearchErrorCode.INVALID_QUERY)
        if self.mode is SearchMode.SAMPLE:
            if (
                (self.sample is None and self.sample_id is None)
                or self.owner_id is None
                or self.text is not None
            ):
                raise SearchError(SearchErrorCode.INVALID_QUERY)
            require_identifier(self.owner_id)
            if self.sample_id is not None:
                require_identifier(self.sample_id)
        elif (
            self.sample is not None
            or self.sample_id is not None
            or self.text is None
            or not self.text.strip()
            or len(self.text) > SearchLimit.QUERY_CHARACTERS
        ):
            raise SearchError(SearchErrorCode.INVALID_QUERY)
