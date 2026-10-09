"""Миграции запускаются отдельным административным процессом."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from memes_processing.infrastructure.config.settings import ProcessingSettings
from memes_processing.infrastructure.persistence.models import Base

settings = ProcessingSettings()
target_metadata = Base.metadata


def offline() -> None:
    """Сформировать SQL без подключения к ресурсу."""
    context.configure(
        url=settings.database_url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    """Применить миграции с конфигурацией того же релиза."""
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.run_sync(configure)
    await engine.dispose()


def configure(connection: object) -> None:
    """Передать соединение в Alembic."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    offline()
else:
    asyncio.run(online())
