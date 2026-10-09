"""Отдельный восстанавливаемый процесс outbox и reaper."""

import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from random import SystemRandom

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import DBAPIError

from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.logging import configure_logging
from memes_processing.application.dto.coordinator import CoordinatorLimit
from memes_processing.application.dto.features import integer, sequence
from memes_processing.application.ports.external import IArtifactStorage, IEventDelivery
from memes_processing.application.ports.repository import ITransaction
from memes_processing.application.use_cases.delivery import CleanArtifact, DeliverOutbox
from memes_processing.application.use_cases.jobs import RecoverExpired
from memes_processing.application.use_cases.sources import ExpirePrivateSources
from memes_processing.infrastructure.config.settings import ProcessingSettings
from memes_processing.infrastructure.http.delivery import RESTDelivery
from memes_processing.infrastructure.persistence.repository import PostgresTransaction
from memes_processing.infrastructure.storage.s3 import S3Artifacts


class ProcessLog(StrEnum):
    """Санитизированные события технического процесса."""

    DELIVERY_RETRY = "processing_delivery_retry event=%s"
    DATABASE_RETRY = "processing_database_retry"


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryBatch:
    """Результат ограниченного прохода без зависимости от длины всей очереди."""

    steps: int
    completed: int

    @property
    def should_continue(self) -> bool:
        """Продолжить полную порцию с прогрессом без фиксированной паузы."""
        return self.steps == CoordinatorLimit.OUTBOX_BATCH and self.completed > 0


async def deliver_batch(
    *,
    transaction: ITransaction,
    storage: IArtifactStorage,
    delivery: IEventDelivery,
    stopped: asyncio.Event,
) -> DeliveryBatch:
    """Обработать ограниченный пакет и вернуть фактический прогресс.

    Args:
        transaction: Прикладной порт коротких транзакций.
        storage: Хранилище удаляемых артефактов.
        delivery: Получатель исходящих событий.
        stopped: Сигнал остановки между внешними операциями.

    Returns:
        DeliveryBatch: Число шагов и успешно завершённых операций.
    """
    steps = completed = 0
    for _ in range(CoordinatorLimit.OUTBOX_BATCH):
        if stopped.is_set():
            break
        cleanup = await transaction.run(CleanArtifact(storage=storage, now=datetime.now(UTC)))
        cleaned = integer(cleanup.get("processed"))
        completed += cleaned
        if stopped.is_set():
            break
        delivered = await transaction.run(DeliverOutbox(delivery=delivery, now=datetime.now(UTC)))
        retried = sequence(delivered.get("retried_event_ids"))
        processed = integer(delivered.get("processed"))
        completed += processed - len(retried)
        for event_id in retried:
            logger.warning(ProcessLog.DELIVERY_RETRY, event_id)
        steps += 1
        if not cleaned and not processed:
            break
    return DeliveryBatch(steps=steps, completed=completed)


async def run() -> None:
    """Доставлять события и возвращать потерянные lease в очередь."""
    settings = ProcessingSettings()
    configure_logging(level=settings.log_level, release_id=settings.release_id)
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_overflow,
    )
    sessions = session_factory(engine)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(shutdown_signal, stopped.set)
        except NotImplementedError:
            signal.signal(
                shutdown_signal, lambda _signal, _frame: loop.call_soon_threadsafe(stopped.set)
            )
    storage = S3Artifacts(settings)
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
            delivery = RESTDelivery(client, settings)
            transaction = PostgresTransaction(sessions)
            while not stopped.is_set():
                try:
                    await transaction.run(ExpirePrivateSources(now=datetime.now(UTC)))
                    await transaction.run(
                        RecoverExpired(
                            policy=settings.policy(),
                            now=datetime.now(UTC),
                            random_fraction=SystemRandom().random(),
                        )
                    )
                    batch = await deliver_batch(
                        transaction=transaction, storage=storage, delivery=delivery, stopped=stopped
                    )
                    if batch.should_continue:
                        await asyncio.sleep(0)
                        continue
                except (DBAPIError, BotoCoreError, ClientError):
                    logger.warning(ProcessLog.DATABASE_RETRY)
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=settings.outbox_interval_seconds)
                except TimeoutError:
                    continue
    finally:
        await storage.close()
        await engine.dispose()


def main() -> None:
    """Запустить отдельный тип процесса coordinator."""
    asyncio.run(run())
