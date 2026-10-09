"""Изолированные реальные PostgreSQL-индексы для регрессий смены версии."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from memes_search.infrastructure.migrations.versions.v0001_search import upgrade
from memes_search.infrastructure.migrations.versions.v0002_search_fences import (
    upgrade as upgrade_fences,
)
from memes_search.infrastructure.migrations.versions.v0003_search_fragments import (
    upgrade as upgrade_fragments,
)
from memes_search.infrastructure.migrations.versions.v0004_shared_sample_admission import (
    upgrade as upgrade_admissions,
)


def migrate(connection: Connection) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        upgrade()
        upgrade_fences()
        upgrade_fragments()
        upgrade_admissions()


@asynccontextmanager
async def isolated_database(url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = "search_test_" + uuid4().hex
    administrative = create_async_engine(url)
    async with administrative.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema + ",public"}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(migrate)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
        async with administrative.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await administrative.dispose()
