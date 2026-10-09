"""Снимки ранжирования для стабильной ограниченной пагинации."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from memes_kernel.values import Number, Revision, require_identifier, require_ordered
from memes_search.application.dto.queries import SearchLimit, SearchQuery
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.domain.ranking import RankedGroup


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupSnapshot:
    """Ревизия состава группы при фиксации результатов."""

    group_id: UUID
    composition_revision: Revision


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchSession:
    """Временный снимок выдачи; единственная копия хранится вне процесса."""

    id: UUID
    query: SearchQuery
    created_at: datetime
    results: tuple[RankedGroup, ...]
    groups: tuple[GroupSnapshot, ...]
    expires_at: datetime
    degraded: bool = False

    def __post_init__(self) -> None:
        """Проверить срок жизни и соответствие групп результатам."""
        require_identifier(self.id)
        require_ordered(self.expires_at, self.created_at)
        result_groups = tuple(item.group_id for item in self.results)
        snapshot_groups = tuple(item.group_id for item in self.groups)
        if (
            self.expires_at <= self.created_at
            or len(set(result_groups)) != len(result_groups)
            or len(set(snapshot_groups)) != len(snapshot_groups)
            or set(result_groups) != set(snapshot_groups)
        ):
            raise SearchError(SearchErrorCode.INVALID_SESSION)

    def require_current(self, *, now: datetime, groups: tuple[GroupSnapshot, ...]) -> None:
        """Проверить срок и актуальный состав возвращаемых групп.

        Args:
            now: Текущее серверное время.
            groups: Ревизии состава групп, известные каталогу.
        """
        require_ordered(now, self.created_at)
        if now >= self.expires_at:
            raise SearchError(SearchErrorCode.SESSION_EXPIRED)
        if set(self.groups) != set(groups):
            raise SearchError(SearchErrorCode.GROUPS_CHANGED)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchSessionPolicy:
    """Ограниченный срок хранения запроса и результатов."""

    lifetime: timedelta = timedelta(seconds=SearchLimit.SESSION_SECONDS)
    maximum_sample_bytes: int = SearchLimit.MAXIMUM_SAMPLE_BYTES
    active_samples_per_user: int = SearchLimit.ACTIVE_SAMPLES_PER_USER
    maximum_query_characters: int = SearchLimit.QUERY_CHARACTERS
    download_seconds: int = SearchLimit.DOWNLOAD_SECONDS

    def __post_init__(self) -> None:
        """Запретить неположительный срок жизни."""
        if (
            self.lifetime <= timedelta()
            or not Number.ONE <= self.maximum_sample_bytes <= SearchLimit.MAXIMUM_SAMPLE_BYTES
            or self.active_samples_per_user < Number.ONE
            or not Number.ONE <= self.maximum_query_characters <= SearchLimit.QUERY_CHARACTERS
            or not Number.ONE <= self.download_seconds <= SearchLimit.DOWNLOAD_SECONDS
        ):
            raise SearchError(SearchErrorCode.INVALID_SESSION)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchPage:
    """Проверенная по каталогу страница снимка."""

    session_id: UUID
    expires_at: datetime
    results: tuple[RankedGroup, ...]
    next_offset: int | None
    degraded: bool
    window_size: int
    offset: int = Number.ZERO
    page_size: int = SearchLimit.DEFAULT_PAGE_SIZE
