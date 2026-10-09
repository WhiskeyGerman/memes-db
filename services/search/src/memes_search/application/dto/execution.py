"""Состояния асинхронного поискового запроса."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from memes_search.application.dto.queries import SearchQuery
from memes_search.application.dto.sessions import SearchPage


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRequest:
    """Приватные параметры и связь с вычислением во внешнем хранилище."""

    id: UUID
    query: SearchQuery
    created_at: datetime
    expires_at: datetime
    inference_task_id: UUID | None = None
    session_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchPending:
    """Ответ ожидающего настоящие ML-признаки запроса."""

    request_id: UUID
    expires_at: datetime
    task_id: UUID | None


SearchExecution = SearchPage | SearchPending
