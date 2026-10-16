"""Отдельный stateless-процесс доставки REST outbox."""

import asyncio
import logging
import signal
from enum import StrEnum

import httpx
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SqlTimeoutError

from memes_identity.application.use_cases.outbox import DeliverAccountDeletion
from memes_identity.infrastructure.clients.account_deletion import RestAccountDeletionPublisher
from memes_identity.infrastructure.clock import SystemClock
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.unit_of_work import SqlIdentityUnitOfWork
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.logging import ServiceName, configure_logging


class OutboxEvent(StrEnum):
    """Безопасные операционные события."""

    STARTED = "identity_outbox_started"
    STOPPED = "identity_outbox_stopped"
    DATABASE_RETRY = "identity_outbox_database_retry"


logger = logging.getLogger(__name__)


async def run(settings: IdentitySettings) -> None:
    """Доставлять события до SIGTERM; HTTP timeout ограничивает завершение.

    Args:
        settings: Конфигурация текущего релиза.
    """
    settings.require_outbox_configuration()
    configure_logging(
        level=settings.log_level,
        release_id=settings.release_id,
        service_name=ServiceName.IDENTITY_OUTBOX,
        environment=settings.deployment_environment,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            signal.signal(signum, lambda _signum, _frame: loop.call_soon_threadsafe(stop.set))
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_overflow,
    )
    factory = session_factory(engine)
    logger.info(OutboxEvent.STARTED)
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
            publisher = RestAccountDeletionPublisher(client, settings)
            while not stop.is_set():
                use_case = DeliverAccountDeletion(
                    uow=SqlIdentityUnitOfWork(factory, settings),
                    publisher=publisher,
                    clock=SystemClock(),
                )
                try:
                    found = await use_case.execute()
                except (DBAPIError, SqlTimeoutError, OSError):
                    found = False
                    logger.warning(OutboxEvent.DATABASE_RETRY, exc_info=True)
                if not found:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=settings.outbox_poll_seconds)
                    except TimeoutError:
                        continue
    finally:
        await engine.dispose()
        logger.info(OutboxEvent.STOPPED)


def main() -> None:
    """Запустить отдельный фоновый процесс текущего релиза."""
    asyncio.run(run(IdentitySettings()))


if __name__ == "__main__":
    main()
