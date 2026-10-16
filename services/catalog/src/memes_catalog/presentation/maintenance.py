"""Отдельный масштабируемый процесс доставки и физической очистки."""

import asyncio
import logging
import signal
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLTimeoutError

from memes_catalog.application.use_cases.delivery import DeliverEvents
from memes_catalog.infrastructure.configuration import CatalogSettings
from memes_catalog.infrastructure.persistence.outbox import PostgresOutbox
from memes_catalog.presentation.main import build_container, close_container
from memes_platform.application.errors import ApplicationError
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.logging import ServiceName, configure_logging
from memes_platform.presentation.process import graceful_batch


class ProcessEvent(StrEnum):
    """Безопасные события процесса."""

    STOP = "catalog_maintenance_stopping"
    RETRY = "catalog_maintenance_dependency_unavailable"


logger = logging.getLogger(__name__)


async def run(settings: CatalogSettings) -> None:
    """Запустить процесс с конечными пакетами и обработкой остановки.

    Args:
        settings: Окружение конкретного релиза.
    """
    configure_logging(
        level=settings.log_level,
        release_id=settings.release_id,
        service_name=ServiceName.CATALOG_MAINTENANCE,
        environment=settings.deployment_environment,
    )
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_overflow,
    )
    dependencies = build_container(settings, engine)
    dispatcher = DeliverEvents(PostgresOutbox(session_factory(engine)), dependencies.delivery)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stop(_number: int, _frame: object) -> None:
        """Уведомить цикл об остановке."""
        loop.call_soon_threadsafe(stopping.set)

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stop)
    try:
        while not stopping.is_set():
            try:
                cleaned = await graceful_batch(
                    dependencies.catalog.maintenance(
                        now=datetime.now(UTC), limit=settings.outbox_batch_size
                    ),
                    stopping,
                    grace_seconds=settings.shutdown_seconds,
                )
                if stopping.is_set():
                    break
                delivered = await graceful_batch(
                    dispatcher.execute(now=datetime.now(UTC), limit=settings.outbox_batch_size),
                    stopping,
                    grace_seconds=settings.shutdown_seconds,
                )
                if cleaned or delivered:
                    continue
            except (ApplicationError, DBAPIError, SQLTimeoutError, OSError):
                logger.warning(ProcessEvent.RETRY, exc_info=True)
            try:
                await asyncio.wait_for(
                    stopping.wait(), timeout=settings.maintenance_interval_seconds
                )
            except TimeoutError:
                continue
    finally:
        logger.info(ProcessEvent.STOP)
        await close_container(dependencies)
        await engine.dispose()


def main() -> None:
    """Запустить отдельный process type без HTTP-сервера."""
    asyncio.run(run(CatalogSettings()))


if __name__ == "__main__":
    main()
