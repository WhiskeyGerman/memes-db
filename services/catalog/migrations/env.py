"""Одноразовый процесс миграции Catalog без запуска HTTP."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from memes_catalog.infrastructure.persistence.models import Base
from memes_platform.infrastructure.configuration import BaseServiceSettings
from memes_platform.infrastructure.database import create_engine


def configure(connection: Connection) -> None:
    """Связать миграции с общей транзакцией.

    Args:
        connection: Подключение PostgreSQL.
    """
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def migrate() -> None:
    """Применить схему в окружении конкретного релиза."""
    settings = BaseServiceSettings()
    engine = create_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(configure)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    settings = BaseServiceSettings()
    context.configure(
        url=settings.database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(migrate())
