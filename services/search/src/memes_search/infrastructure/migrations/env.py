"""Среда Alembic с явным asyncpg подключением и без autostart миграций."""

import asyncio

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from memes_search.infrastructure.persistence.sample_admission_models import SampleAdmissionRow

configuration = context.config


def offline() -> None:
    """Сформировать SQL релиза без подключения к backing service."""
    context.configure(
        url=configuration.get_main_option("sqlalchemy.url"),
        target_metadata=SampleAdmissionRow.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def configure(connection: Connection) -> None:
    """Применить миграции в административной транзакции."""
    context.configure(connection=connection, target_metadata=SampleAdmissionRow.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    """Подключить отдельный административный процесс к PostgreSQL."""
    engine = async_engine_from_config(
        configuration.get_section(configuration.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(configure)
    await engine.dispose()


if context.is_offline_mode():
    offline()
else:
    asyncio.run(online())
