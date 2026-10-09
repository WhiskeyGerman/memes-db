"""Readiness административного процесса без импорта бизнес-слоёв."""

import asyncio
from enum import IntEnum

from sqlalchemy import literal, select

from memes_platform.infrastructure.configuration import BaseServiceSettings
from memes_platform.infrastructure.database import create_engine


class ProbeLimit(IntEnum):
    """Ограниченный ресурс health-процесса."""

    TIMEOUT_SECONDS = 5
    POOL_SIZE = 1
    POOL_OVERFLOW = 0
    VALUE = 1


async def ready() -> None:
    """Проверить внешний PostgreSQL и сразу освободить соединение."""
    settings = BaseServiceSettings()
    engine = create_engine(
        settings.database_url,
        pool_size=ProbeLimit.POOL_SIZE,
        max_overflow=ProbeLimit.POOL_OVERFLOW,
    )
    try:
        async with asyncio.timeout(ProbeLimit.TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(select(literal(int(ProbeLimit.VALUE))))
    finally:
        await engine.dispose()


def main() -> None:
    """Завершиться ненулевым кодом, если подключаемый ресурс недоступен."""
    asyncio.run(ready())


if __name__ == "__main__":
    main()
