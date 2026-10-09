"""Фабрики подключения; миграции не запускаются при старте API."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as sqlalchemy_create_async_engine

from memes_platform.infrastructure.configuration import RuntimeDefault


def create_engine(
    database_url: str,
    *,
    pool_size: int = RuntimeDefault.DATABASE_POOL_SIZE,
    max_overflow: int = RuntimeDefault.DATABASE_POOL_OVERFLOW,
) -> AsyncEngine:
    """Создать ограниченный пул PostgreSQL.

    Args:
        database_url: DSN PostgreSQL/asyncpg.
        pool_size: Базовый размер пула.
        max_overflow: Дополнительные подключения при всплеске.
    """
    return sqlalchemy_create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        hide_parameters=True,
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Создать фабрику независимых транзакционных сессий.

    Args:
        engine: Пул текущего процесса.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
