"""Совместимые импорты прикладных моделей поиска."""

from memes_search.application.dto.queries import SearchLimit, SearchMode, SearchQuery
from memes_search.application.dto.sessions import GroupSnapshot, SearchSession, SearchSessionPolicy
from memes_search.application.errors import SearchError, SearchErrorCode

__all__ = [
    "GroupSnapshot",
    "SearchError",
    "SearchErrorCode",
    "SearchLimit",
    "SearchMode",
    "SearchQuery",
    "SearchSession",
    "SearchSessionPolicy",
]
