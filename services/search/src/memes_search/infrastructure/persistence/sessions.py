"""Персистентные снимки и приватные параметры асинхронного поиска."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.execution import SearchRequest
from memes_search.application.dto.sessions import SearchSession
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.requests import ISearchRequestRepository
from memes_search.application.ports.sessions import ISearchSessionRepository
from memes_search.infrastructure.persistence.codecs import (
    REQUEST,
    SESSION,
    request_body,
    session_body,
)
from memes_search.infrastructure.persistence.models import RequestRow, SessionRow
from memes_search.infrastructure.persistence.owner_guard import lock_owner, require_active_owner


class PostgreSQLSessions(ISearchSessionRepository):
    """Внешнее хранилище неизменяемых снимков выдачи."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Сохранить фабрику независимых транзакций."""
        self._factory = factory

    async def get(self, session_id: UUID) -> SearchSession | None:
        """Прочитать снимок по непрозрачному идентификатору."""
        async with self._factory() as session:
            row = await session.get(SessionRow, session_id)
            return SESSION.validate_python(row.body) if row is not None else None

    async def save(self, session: SearchSession) -> None:
        """Сохранить снимок единожды без перезаписи результата."""
        async with self._factory() as database, database.begin():
            await lock_owner(database, session.query.owner_id)
            await require_active_owner(database, session.query.owner_id)
            await database.execute(
                insert(SessionRow)
                .values(
                    id=session.id,
                    body=session_body(session),
                    expires_at=session.expires_at,
                )
                .on_conflict_do_nothing(index_elements=[SessionRow.id])
            )

    async def remove_expired(self, now: datetime) -> None:
        """Удалить просроченные запросы и окна результатов."""
        async with self._factory() as session, session.begin():
            await session.execute(delete(SessionRow).where(SessionRow.expires_at <= now))


class PostgreSQLRequests(ISearchRequestRepository):
    """Сохранение поиска, ожидающего ответ inference-пула."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Сохранить фабрику транзакций PostgreSQL."""
        self._factory = factory

    async def get(self, request_id: UUID) -> SearchRequest | None:
        """Прочитать приватные параметры и идентификатор результата."""
        async with self._factory() as session:
            row = await session.get(RequestRow, request_id)
            return REQUEST.validate_python(row.body) if row is not None else None

    async def save(self, request: SearchRequest) -> None:
        """Атомарно сохранить параметры и результат этапа запроса."""
        async with self._factory() as session, session.begin():
            await lock_owner(session, request.query.owner_id)
            await require_active_owner(session, request.query.owner_id)
            inserted = await session.scalar(
                insert(RequestRow)
                .values(
                    id=request.id,
                    body=request_body(request),
                    expires_at=request.expires_at,
                )
                .on_conflict_do_nothing(index_elements=[RequestRow.id])
                .returning(RequestRow.id)
            )
            if inserted is not None:
                return
            row = await session.scalar(
                select(RequestRow).where(RequestRow.id == request.id).with_for_update()
            )
            if row is None:
                raise SearchError(SearchErrorCode.NOT_FOUND)
            current = REQUEST.validate_python(row.body)
            if current.query != request.query or current.created_at != request.created_at:
                raise SearchError(SearchErrorCode.INVALID_QUERY)
            if current.session_id is None:
                row.body = request_body(request)

    async def remove_expired(self, now: datetime) -> None:
        """Удалить просроченные приватные тексты запросов."""
        async with self._factory() as session, session.begin():
            await session.execute(delete(RequestRow).where(RequestRow.expires_at <= now))
