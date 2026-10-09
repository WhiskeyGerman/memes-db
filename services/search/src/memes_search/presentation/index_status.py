"""Одноразовый административный процесс измерения покрытия индекса."""

import asyncio
import json
import sys

from fastapi.encoders import jsonable_encoder

from memes_platform.infrastructure.database import create_engine, session_factory
from memes_search.application.use_cases.index_status import InspectIndex
from memes_search.infrastructure.clock import SystemClock
from memes_search.infrastructure.configuration import Settings
from memes_search.infrastructure.persistence.index_status import PostgreSQLIndexStatus


async def run() -> None:
    """Прочитать индекс и вывести один JSON-документ для runbook."""
    settings = Settings()
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_overflow,
    )
    try:
        status = await InspectIndex(
            reader=PostgreSQLIndexStatus(session_factory(engine)),
            clock=SystemClock(),
            accepted_version=settings.accepted_embedding_version,
            release_id=settings.release_id,
        ).execute()
        sys.stdout.write(
            json.dumps(jsonable_encoder(status), ensure_ascii=False, sort_keys=True) + "\n"
        )
    finally:
        await engine.dispose()


def main() -> None:
    """Запустить read-only admin command из артефакта релиза."""
    asyncio.run(run())
