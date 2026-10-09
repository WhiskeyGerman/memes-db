"""Входные схемы поиска и сериализация публичной выдачи."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from memes_search.application.dto.execution import SearchPending
from memes_search.application.dto.queries import (
    SearchFilters,
    SearchLimit,
    SearchMediaKind,
    SearchMode,
    SearchQuery,
)
from memes_search.application.dto.sessions import SearchPage
from memes_search.presentation.links import SearchResourceLinks
from memes_search.presentation.schemas.features import TimeSpanBody


class SearchBody(BaseModel):
    """Вход публичного поиска без серверных идентификаторов владельца."""

    request_id: UUID | None = None
    mode: SearchMode = SearchMode.HYBRID
    text: str | None = Field(default=None, max_length=SearchLimit.QUERY_CHARACTERS)
    sample_id: UUID | None = None
    page_size: int = Field(
        default=SearchLimit.DEFAULT_PAGE_SIZE, ge=1, le=SearchLimit.MAXIMUM_PAGE_SIZE
    )
    kinds: list[SearchMediaKind] = Field(default_factory=list)
    published_after: datetime | None = None
    published_before: datetime | None = None

    def query(self, owner_id: UUID | None) -> SearchQuery:
        """Преобразовать контролируемый транспорт в application DTO."""
        return SearchQuery(
            mode=self.mode,
            text=self.text,
            sample_id=self.sample_id,
            owner_id=owner_id,
            page_size=self.page_size,
            filters=SearchFilters(
                kinds=tuple(self.kinds),
                published_after=self.published_after,
                published_before=self.published_before,
            ),
        )


class RankedItemBody(BaseModel):
    """Публичный представитель одной актуальной группы."""

    group_id: UUID
    representative_id: UUID
    score: float
    matched_span: TimeSpanBody | None = None


class SearchPendingBody(BaseModel):
    """Ожидание ML с адресом создавшего ресурс индекса."""

    request_id: UUID
    expires_at: datetime
    task_id: UUID | None
    resource_url: str

    @classmethod
    def from_pending(
        cls, pending: SearchPending, links: SearchResourceLinks
    ) -> "SearchPendingBody":
        """Закрепить polling независимо от generic router."""
        return cls(
            request_id=pending.request_id,
            expires_at=pending.expires_at,
            task_id=pending.task_id,
            resource_url=links.request(pending.request_id),
        )


class SearchPageBody(BaseModel):
    """Ограниченное окно выдачи без обещания точного общего числа."""

    resource_url: str
    next_page_url: str | None
    session_id: UUID
    expires_at: datetime
    results: list[RankedItemBody]
    next_offset: int | None
    degraded: bool
    limitation: str | None
    window_size: int

    @classmethod
    def from_page(cls, page: SearchPage, links: SearchResourceLinks) -> "SearchPageBody":
        """Сериализовать проверенный сценариями снимок."""
        return cls(
            resource_url=links.page(page.session_id, page.offset, page.page_size),
            next_page_url=links.page(page.session_id, page.next_offset, page.page_size)
            if page.next_offset is not None
            else None,
            session_id=page.session_id,
            expires_at=page.expires_at,
            results=[
                RankedItemBody(
                    group_id=item.group_id,
                    representative_id=item.representative_id,
                    score=item.score,
                    matched_span=TimeSpanBody(
                        start_ms=item.matched_span.start_ms, end_ms=item.matched_span.end_ms
                    )
                    if item.matched_span
                    else None,
                )
                for item in page.results
            ],
            next_offset=page.next_offset,
            degraded=page.degraded,
            limitation="semantic_search_unavailable" if page.degraded else None,
            window_size=page.window_size,
        )
