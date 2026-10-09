import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from memes_worker.application.dto.runtime import (
    Payload,
    WorkAssignment,
    WorkerFailure,
    WorkerTransportError,
    WorkResult,
)
from memes_worker.application.ports.client import ICoordinatorClient
from memes_worker.application.ports.execution import ITaskExecutor
from memes_worker.application.use_cases.runner import WorkerRunner
from memes_worker.infrastructure.cache.sqlite import SQLiteCache


class RecordingClient(ICoordinatorClient):
    def __init__(self, *, lose_lease: bool = False) -> None:
        self.lose_lease = lose_lease
        self.submitted: list[WorkResult] = []

    async def poll(self, capabilities: Payload) -> WorkAssignment | None:
        return None

    async def revocations(
        self, capabilities: Payload, source_ids: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        return ()

    async def renew(self, work: WorkAssignment) -> None:
        if self.lose_lease:
            raise WorkerTransportError(WorkerFailure.OWNERSHIP_LOST)

    async def submit(self, work: WorkAssignment, result: WorkResult) -> None:
        self.submitted.append(result)

    async def fail(self, work: WorkAssignment, reason: WorkerFailure) -> None:
        return None


class RecordingExecutor(ITaskExecutor):
    def __init__(self, result: WorkResult, *, blocked: bool = False) -> None:
        self.result = result
        self.blocked = blocked
        self.calls = 0
        self.cancelled = False

    async def execute(self, work: WorkAssignment) -> WorkResult:
        self.calls += 1
        try:
            if self.blocked:
                await asyncio.Event().wait()
            return self.result
        finally:
            self.cancelled = self.blocked


@pytest.mark.asyncio
async def test_cached_result_is_resent_without_inference(
    work: WorkAssignment, result: WorkResult, cache: SQLiteCache
) -> None:
    await cache.put(result, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    client = RecordingClient()
    executor = RecordingExecutor(result)
    await WorkerRunner(client, executor, cache, {}).execute(work)
    assert executor.calls == 0
    assert client.submitted == [result]


@pytest.mark.asyncio
async def test_ownership_loss_stops_model_and_discards_source_cache(
    work: WorkAssignment, result: WorkResult, cache: SQLiteCache
) -> None:
    client = RecordingClient(lose_lease=True)
    executor = RecordingExecutor(result, blocked=True)
    with pytest.raises(WorkerTransportError) as error:
        await WorkerRunner(client, executor, cache, {}, renewal_seconds=0.01).execute(work)
    assert error.value.reason is WorkerFailure.OWNERSHIP_LOST
    assert executor.cancelled
    assert client.submitted == []
    assert await cache.get(result.key, now=datetime.now(UTC)) is None
