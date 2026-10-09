"""Единая транзакционная сериализация записей приватного владельца."""

from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.infrastructure.persistence.models import DeletedOwnerRow


class OwnerSql(StrEnum):
    """Каноническое пространство advisory lock всех private adapters."""

    LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"
    PREFIX = "search/private-owner/"


async def lock_owner(session: AsyncSession, owner_id: UUID | None) -> None:
    """Сериализовать любые private записи с одной операцией удаления."""
    if owner_id is not None:
        await session.execute(text(OwnerSql.LOCK), {"owner": f"{OwnerSql.PREFIX}{owner_id}"})


async def require_active_owner(session: AsyncSession, owner_id: UUID | None) -> None:
    """Отбить позднюю запись после удаления; вызывается под owner lock."""
    if owner_id is not None and await session.get(DeletedOwnerRow, owner_id) is not None:
        raise SearchError(SearchErrorCode.FORBIDDEN)
