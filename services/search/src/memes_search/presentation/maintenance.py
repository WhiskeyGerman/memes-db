"""Сборка и graceful shutdown отдельного maintenance-процесса."""

import asyncio
import logging
import signal
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError

from memes_kernel.values import Number
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.http import InternalHttpClient
from memes_platform.infrastructure.logging import configure_logging
from memes_search.application.dto.delivery import DeliveryPolicy, MaintenanceResult
from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.use_cases.maintenance import SearchMaintenance
from memes_search.infrastructure.clients.inference import ProcessingRestGateway
from memes_search.infrastructure.clock import SystemClock
from memes_search.infrastructure.configuration import SearchDefault, Settings
from memes_search.infrastructure.persistence.outbox import PostgreSQLOutbox
from memes_search.infrastructure.persistence.samples import PostgreSQLSamples
from memes_search.infrastructure.persistence.sessions import PostgreSQLRequests, PostgreSQLSessions
from memes_search.infrastructure.storage import S3PrivateStorage
from memes_search.presentation.admission import create_admission

logger = logging.getLogger(__name__)


async def drain(
    maintenance: SearchMaintenance, stop: asyncio.Event
) -> tuple[MaintenanceResult, MaintenanceResult]:
    """Разобрать готовый backlog порциями, уступая управление между проходами."""
    delivered = cleaned = MaintenanceResult(completed=Number.ZERO, failed=Number.ZERO)
    while not stop.is_set():
        cleaned = await maintenance.clean()
        if not cleaned.continue_immediately:
            break
        await asyncio.sleep(Number.ZERO)
    if not stop.is_set():
        delivered = await maintenance.deliver()
    return delivered, cleaned


async def run() -> None:
    """Выполнить отдельный disposability-процесс с сигналом остановки."""
    settings = Settings()
    configure_logging(level=settings.log_level, release_id=settings.release_id)
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_overflow,
    )
    factory = session_factory(engine)
    client = InternalHttpClient(
        base_url=settings.processing_url,
        internal_api_key=settings.internal_api_key,
        timeout=settings.http_timeout_seconds,
    )
    inference = ProcessingRestGateway(
        client,
        wait_seconds=Number.ZERO,
        poll_seconds=settings.query_poll_milliseconds / SearchDefault.MILLISECONDS_PER_SECOND,
        dimensions=settings.embedding_dimensions,
        space_id=settings.embedding_space_id,
        version=settings.accepted_embedding_version,
    )
    clock = SystemClock()
    admission, admission_client = create_admission(
        settings,
        factory,
        clock,
        SearchSessionPolicy(
            lifetime=timedelta(seconds=settings.sample_session_seconds),
            active_samples_per_user=settings.active_samples_per_user,
        ),
    )
    storage = S3PrivateStorage(settings)
    maintenance = SearchMaintenance(
        outbox=PostgreSQLOutbox(factory),
        samples=PostgreSQLSamples(factory),
        admission=admission,
        sessions=PostgreSQLSessions(factory),
        requests=PostgreSQLRequests(factory),
        storage=storage,
        inference=inference,
        clock=clock,
        policy=DeliveryPolicy(
            batch=settings.delivery_batch,
            lease=timedelta(seconds=settings.delivery_lease_seconds),
            initial_retry=timedelta(seconds=settings.delivery_retry_seconds),
            maximum_retry=timedelta(seconds=settings.delivery_max_retry_seconds),
        ),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def shutdown_handler(signum: int, frame: object) -> None:
        """Перевести SIGTERM в остановку после текущей транзакции."""
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    try:
        while not stop.is_set():
            delivered = MaintenanceResult(completed=Number.ZERO, failed=Number.ZERO)
            try:
                delivered, cleaned = await drain(maintenance, stop)
                if delivered.failed or cleaned.failed:
                    logger.warning(
                        "search_maintenance_retry delivery=%d cleanup=%d",
                        delivered.failed,
                        cleaned.failed,
                    )
            except SQLAlchemyError:
                logger.warning("search_maintenance_database_unavailable")
            if delivered.continue_immediately:
                await asyncio.sleep(Number.ZERO)
                continue
            try:
                await asyncio.wait_for(stop.wait(), settings.maintenance_interval_seconds)
            except TimeoutError:
                continue
    finally:
        await client.close()
        if admission_client is not None:
            await admission_client.close()
        storage.close()
        await engine.dispose()


def main() -> None:
    """Запустить maintenance-процесс из неизменяемого артефакта."""
    asyncio.run(run())
