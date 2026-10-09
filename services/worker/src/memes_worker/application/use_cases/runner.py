"""Исполнение, heartbeat, отмена и повторная доставка."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum

from memes_worker.application.dto.runtime import (
    Payload,
    WorkAssignment,
    WorkerExecutionError,
    WorkerFailure,
    WorkerTransportError,
    WorkResult,
)
from memes_worker.application.execution import WorkerLimit
from memes_worker.application.ports.client import ICoordinatorClient
from memes_worker.application.ports.execution import IResultCache, ITaskExecutor


class RunnerLimit(IntEnum):
    """Периоды управления процессом."""

    CLEANUP_SECONDS = 60
    RETRY_SECONDS = 5


class RunnerLog(StrEnum):
    """События без содержимого медиа."""

    TRANSPORT_RETRY = "worker_transport_retry"
    EXECUTION_FAILED = "worker_execution_failed task=%s reason=%s"
    CACHE_FULL = "worker_cache_full"


logger = logging.getLogger(__name__)


class WorkerRunner:
    """Право назначения определяется только сервером."""

    def __init__(
        self,
        client: ICoordinatorClient,
        executor: ITaskExecutor,
        cache: IResultCache,
        capabilities: Payload,
        *,
        renewal_seconds: float = WorkerLimit.RENEWAL_SECONDS,
        cache_seconds: int = WorkerLimit.CACHE_SECONDS,
    ) -> None:
        """Принять порты и параметры релиза."""
        self.client, self.executor, self.cache = client, executor, cache
        self.capabilities = capabilities
        self.renewal_seconds, self.cache_seconds = renewal_seconds, cache_seconds

    async def run(self, stopped: asyncio.Event, *, concurrency: int) -> None:
        """Исполнять независимые слоты до сигнала завершения."""
        await self.cache.purge(now=datetime.now(UTC))
        cleanup = asyncio.create_task(self._cleanup(stopped))
        slots = [asyncio.create_task(self._slot(stopped)) for _ in range(concurrency)]
        stop = asyncio.create_task(stopped.wait())
        try:
            done, _pending = await asyncio.wait(
                {*slots, cleanup, stop},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task is not stop:
                    await task
        finally:
            stop.cancel()
            for task in [*slots, cleanup]:
                task.cancel()
            for task in [*slots, cleanup]:
                try:
                    await task
                except asyncio.CancelledError:
                    continue
            await self.cache.purge(now=datetime.now(UTC))

    async def _cleanup(self, stopped: asyncio.Event) -> None:
        """Очистка выполняется независимо от GPU."""
        while not stopped.is_set():
            await self.cache.purge(now=datetime.now(UTC))
            source_ids = await self.cache.source_ids()
            try:
                revoked = await self.client.revocations(self.capabilities, source_ids)
            except WorkerTransportError as error:
                logger.warning(RunnerLog.TRANSPORT_RETRY)
                revoked = source_ids if error.reason is WorkerFailure.OWNERSHIP_LOST else ()
            for source_id in revoked:
                await self.cache.revoke(source_id)
            await self._pause(stopped, min(RunnerLimit.CLEANUP_SECONDS, self.renewal_seconds))

    async def _slot(self, stopped: asyncio.Event) -> None:
        """Устройство можно потерять без потери серверных задач."""
        while not stopped.is_set():
            await self.cache.purge(now=datetime.now(UTC))
            if await self.cache.full():
                logger.warning(RunnerLog.CACHE_FULL)
                await self._pause(stopped, RunnerLimit.RETRY_SECONDS)
                continue
            try:
                work = await self.client.poll(self.capabilities)
                if work is not None:
                    await self.execute(work)
            except WorkerTransportError:
                logger.warning(RunnerLog.TRANSPORT_RETRY)
                await self._pause(stopped, RunnerLimit.RETRY_SECONDS)

    async def execute(self, work: WorkAssignment) -> None:
        """После потери lease отменить вычисление и удалить кэш."""
        inference = asyncio.create_task(self._compute(work))
        heartbeat = asyncio.create_task(self._renew(work))
        try:
            done, _pending = await asyncio.wait(
                {inference, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                await heartbeat
                raise WorkerTransportError(WorkerFailure.OWNERSHIP_LOST)
            await self.client.submit(work, await inference)
        except WorkerExecutionError as error:
            if error.reason is WorkerFailure.OWNERSHIP_LOST:
                await self.cache.revoke(work.assignment.key.source_id)
                raise WorkerTransportError(WorkerFailure.OWNERSHIP_LOST) from error
            logger.warning(RunnerLog.EXECUTION_FAILED, work.assignment.task_id, error.reason.value)
            await self.client.fail(work, error.reason)
        except WorkerTransportError as error:
            if error.reason is WorkerFailure.OWNERSHIP_LOST:
                await self.cache.revoke(work.assignment.key.source_id)
            elif error.reason is WorkerFailure.INVALID_RESPONSE:
                await self.client.fail(work, WorkerFailure.INVALID_INPUT)
                await self.cache.revoke(work.assignment.key.source_id)
            raise
        finally:
            for task in (inference, heartbeat):
                if not task.done():
                    task.cancel()
            for task in (inference, heartbeat):
                try:
                    await task
                except (asyncio.CancelledError, WorkerTransportError, WorkerExecutionError):
                    continue

    async def _compute(self, work: WorkAssignment) -> WorkResult:
        """Полный ключ предотвращает перенос результата между источниками."""
        cached = await self.cache.get(work.assignment.key, now=datetime.now(UTC))
        if cached is not None:
            return cached
        result = await self.executor.execute(work)
        await self.cache.put(
            result, expires_at=datetime.now(UTC) + timedelta(seconds=self.cache_seconds)
        )
        return result

    async def _renew(self, work: WorkAssignment) -> None:
        """Heartbeat независим от времени вычисления."""
        while True:
            await asyncio.sleep(self.renewal_seconds)
            await self.client.renew(work)

    @staticmethod
    async def _pause(stopped: asyncio.Event, seconds: float) -> None:
        """Завершение немедленно прерывает ожидание."""
        try:
            await asyncio.wait_for(stopped.wait(), timeout=seconds)
        except TimeoutError:
            return
