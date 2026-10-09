"""Одноразовый async Alembic process в окружении Identity."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.models import IdentityBase
from memes_platform.infrastructure.database import create_engine


def configure(connection: Connection) -> None:
    """Применить миграции в одной управляемой транзакции.

    Args:
        connection: Подключение PostgreSQL.
    """
    context.configure(
        connection=connection,
        target_metadata=IdentityBase.metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def migrate_online() -> None:
    """Запустить миграции с текущими параметрами подключаемой БД."""
    settings = IdentitySettings()
    engine = create_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(configure)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    settings = IdentitySettings()
    context.configure(
        url=settings.database_url,
        target_metadata=IdentityBase.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(migrate_online())
