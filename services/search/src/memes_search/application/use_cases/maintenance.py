"""Прикладные сценарии доставки и удаления приватных данных."""

import asyncio

from memes_kernel.values import Number
from memes_search.application.dto.delivery import (
    DeliveryLimit,
    DeliveryMessage,
    DeliveryPolicy,
    MaintenanceResult,
    OutboxAction,
)
from memes_search.application.dto.samples import SampleState
from memes_search.application.errors import SearchError
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.inference import IInferenceGateway
from memes_search.application.ports.outbox import IOutboxRepository
from memes_search.application.ports.requests import ISearchRequestRepository
from memes_search.application.ports.sample_admission import ISampleAdmission
from memes_search.application.ports.samples import ISampleRepository
from memes_search.application.ports.sessions import ISearchSessionRepository
from memes_search.application.ports.storage import IPrivateStorage


class SearchMaintenance:
    """Оркестрация REST и TTL через прикладные порты."""

    def __init__(
        self,
        *,
        outbox: IOutboxRepository,
        samples: ISampleRepository,
        admission: ISampleAdmission,
        sessions: ISearchSessionRepository,
        requests: ISearchRequestRepository,
        storage: IPrivateStorage,
        inference: IInferenceGateway,
        clock: IClock,
        policy: DeliveryPolicy,
    ) -> None:
        """Получить внешние адаптеры и явную политику фоновой работы."""
        self._outbox = outbox
        self._samples = samples
        self._admission = admission
        self._sessions = sessions
        self._requests = requests
        self._storage = storage
        self._inference = inference
        self._clock = clock
        self._policy = policy

    async def deliver(self) -> MaintenanceResult:
        """Доставить ограниченную порцию после фиксации leases."""
        now = self._clock.now()
        messages = await self._outbox.claim(now, now + self._policy.lease, self._policy.batch)
        completed: int = Number.ZERO
        failed: int = Number.ZERO
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + self._policy.lease.total_seconds() / DeliveryLimit.PASS_LEASE_DIVISOR
        )
        for message in messages:
            remaining = deadline - loop.time()
            if remaining <= Number.ZERO:
                break
            try:
                async with asyncio.timeout(remaining):
                    await self._deliver(message)
            except (SearchError, TimeoutError):
                attempts = message.attempts + Number.ONE
                await self._outbox.retry(
                    message.id,
                    message.claim_id,
                    attempts,
                    self._clock.now() + self._policy.retry_delay(attempts),
                )
                failed += Number.ONE
            else:
                await self._outbox.acknowledge(message.id, message.claim_id)
                completed += Number.ONE
        return MaintenanceResult(
            completed=completed,
            failed=failed,
            continue_immediately=completed > Number.ZERO
            and completed + failed >= self._policy.batch,
        )

    async def _deliver(self, message: DeliveryMessage) -> None:
        """Исполнить одно типизированное действие без доступа к SQL."""
        if message.action is OutboxAction.CANCEL_QUERY:
            if message.task_id is not None:
                await self._inference.cancel_query(message.task_id)
        elif message.action is OutboxAction.DELETE_OBJECT:
            if message.object_key is not None:
                await self._storage.delete(message.object_key)
        elif message.action is OutboxAction.CANCEL:
            if message.sample_id is not None:
                await self._inference.cancel_sample(message.sample_id)
        elif message.sample is not None:
            current = await self._samples.get(message.sample.id)
            if (
                current is None
                or current.state is SampleState.CANCELLED
                or current.expires_at <= self._clock.now()
            ):
                await self._inference.cancel_sample(message.sample.id)
            else:
                await self._inference.submit_sample(current)

    async def clean(self) -> MaintenanceResult:
        """Отозвать просроченную обработку и стереть данные по TTL."""
        now = self._clock.now()
        completed: int = Number.ZERO
        failed: int = Number.ZERO
        for sample in await self._samples.expired(now, self._policy.batch):
            try:
                await self._samples.schedule_cancel(sample.id, now)
                await self._storage.delete(sample.object_key)
                await self._samples.remove(sample.id)
            except SearchError:
                failed += Number.ONE
            else:
                completed += Number.ONE
        await self._admission.purge(now)
        await self._sessions.remove_expired(now)
        await self._requests.remove_expired(now)
        return MaintenanceResult(
            completed=completed,
            failed=failed,
            continue_immediately=completed > Number.ZERO
            and completed + failed >= self._policy.batch,
        )
