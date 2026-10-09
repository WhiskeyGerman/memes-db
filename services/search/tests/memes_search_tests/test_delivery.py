"""Leases проверяются реальным PostgreSQL, HTTP выполняется после commit."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.delivery import DeliveryPolicy, OutboxAction
from memes_search.application.dto.samples import PrivateSample
from memes_search.application.use_cases.maintenance import SearchMaintenance
from memes_search.infrastructure.persistence.models import OutboxRow, SampleRow
from memes_search.infrastructure.persistence.outbox import PostgreSQLOutbox
from memes_search.infrastructure.persistence.samples import PostgreSQLSamples
from memes_search.infrastructure.persistence.sessions import PostgreSQLRequests, PostgreSQLSessions
from memes_search.presentation.maintenance import drain
from memes_search_tests.fakes import Inference
from memes_search_tests.harness import SearchHarness

pytestmark = pytest.mark.integration


class LockCheckingInference(Inference):
    def __init__(self, database: async_sessionmaker[AsyncSession], message_id: UUID) -> None:
        super().__init__()
        self.database = database
        self.message_id = message_id
        self.cancelled: list[UUID] = []

    async def cancel_query(self, task_id: UUID) -> None:
        async with self.database() as transaction, transaction.begin():
            row = await transaction.scalar(
                select(OutboxRow)
                .where(OutboxRow.id == self.message_id)
                .with_for_update(nowait=True)
            )
            assert row is not None
        if self.failure:
            await self.query_result(task_id)
        self.cancelled.append(task_id)


@pytest.fixture
def delivery_policy() -> DeliveryPolicy:
    return DeliveryPolicy(
        batch=10,
        lease=timedelta(seconds=60),
        initial_retry=timedelta(seconds=7),
        maximum_retry=timedelta(seconds=30),
    )


async def test_expired_lease_can_be_reclaimed_and_late_ack_does_not_drop_message(
    database: async_sessionmaker[AsyncSession],
    now: datetime,
) -> None:
    repository = PostgreSQLOutbox(database)
    identifier = uuid4()
    async with database() as transaction, transaction.begin():
        transaction.add(
            OutboxRow(
                id=identifier,
                task_id=uuid4(),
                action=OutboxAction.CANCEL_QUERY,
                body={},
                attempts=0,
                available_at=now,
            )
        )
    first = (await repository.claim(now, now + timedelta(seconds=1), 1))[0]
    assert not await repository.claim(now, now + timedelta(seconds=1), 1)
    second = (await repository.claim(now + timedelta(seconds=2), now + timedelta(seconds=3), 1))[0]
    await repository.acknowledge(identifier, first.claim_id)
    await repository.retry(identifier, first.claim_id, 99, now)
    async with database() as transaction:
        row = await transaction.get(OutboxRow, identifier)
        assert row is not None and row.claim_id == second.claim_id and row.attempts == 0
    await repository.acknowledge(identifier, second.claim_id)
    async with database() as transaction:
        assert await transaction.get(OutboxRow, identifier) is None


async def test_retry_is_configurable_and_remote_io_holds_no_row_lock(
    database: async_sessionmaker[AsyncSession],
    now: datetime,
    harness: SearchHarness,
    delivery_policy: DeliveryPolicy,
) -> None:
    message_id, task_id = uuid4(), uuid4()
    async with database() as transaction, transaction.begin():
        transaction.add(
            OutboxRow(
                id=message_id,
                task_id=task_id,
                action=OutboxAction.CANCEL_QUERY,
                body={},
                attempts=0,
                available_at=now,
            )
        )
    inference = LockCheckingInference(database, message_id)
    inference.failure = True
    maintenance = SearchMaintenance(
        outbox=PostgreSQLOutbox(database),
        samples=PostgreSQLSamples(database),
        admission=harness.admission,
        sessions=PostgreSQLSessions(database),
        requests=PostgreSQLRequests(database),
        storage=harness.storage,
        inference=inference,
        clock=harness.clock,
        policy=delivery_policy,
    )
    result = await maintenance.deliver()
    assert result.failed == 1 and result.completed == 0
    async with database() as transaction:
        row = await transaction.get(OutboxRow, message_id)
        assert (
            row is not None
            and row.available_at == now + timedelta(seconds=7)
            and row.claim_id is None
        )
    assert (await maintenance.deliver()).completed == 0
    harness.clock.value += timedelta(seconds=7)
    inference.failure = False
    assert (await maintenance.deliver()).completed == 1
    assert inference.cancelled == [task_id]


async def test_two_maintenance_replicas_claim_each_message_once(
    database: async_sessionmaker[AsyncSession],
    now: datetime,
) -> None:
    identifiers = {uuid4() for _ in range(8)}
    async with database() as transaction, transaction.begin():
        transaction.add_all(
            OutboxRow(
                id=identifier,
                task_id=uuid4(),
                action=OutboxAction.CANCEL_QUERY,
                body={},
                attempts=0,
                available_at=now,
            )
            for identifier in identifiers
        )
    repository = PostgreSQLOutbox(database)
    first, second = await asyncio.gather(
        repository.claim(now, now + timedelta(seconds=60), 8),
        repository.claim(now, now + timedelta(seconds=60), 8),
    )
    first_ids, second_ids = {message.id for message in first}, {message.id for message in second}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == identifiers


class CleanupCheckingInference(Inference):
    def __init__(self, database: async_sessionmaker[AsyncSession], harness: SearchHarness) -> None:
        super().__init__()
        self.database = database
        self.harness = harness
        self.cancelled_samples: list[UUID] = []

    async def cancel_sample(self, sample_id: UUID) -> None:
        async with self.database() as session:
            assert not (await session.scalars(select(SampleRow))).all()
        assert not self.harness.storage.items
        if self.failure:
            await self.query_result(sample_id)
        self.cancelled_samples.append(sample_id)


@pytest.fixture
def cleanup_inference(
    database: async_sessionmaker[AsyncSession], harness: SearchHarness
) -> CleanupCheckingInference:
    return CleanupCheckingInference(database, harness)


@pytest.fixture
def cleanup_maintenance(
    database: async_sessionmaker[AsyncSession],
    harness: SearchHarness,
    delivery_policy: DeliveryPolicy,
    cleanup_inference: CleanupCheckingInference,
) -> SearchMaintenance:
    return SearchMaintenance(
        outbox=PostgreSQLOutbox(database),
        samples=PostgreSQLSamples(database),
        admission=harness.admission,
        sessions=PostgreSQLSessions(database),
        requests=PostgreSQLRequests(database),
        storage=harness.storage,
        inference=cleanup_inference,
        clock=harness.clock,
        policy=delivery_policy,
    )


async def test_local_backlog_is_drained_before_first_processing_io_and_cancellation_survives(
    database: async_sessionmaker[AsyncSession],
    private_sample: PrivateSample,
    harness: SearchHarness,
    delivery_policy: DeliveryPolicy,
    cleanup_inference: CleanupCheckingInference,
    cleanup_maintenance: SearchMaintenance,
) -> None:
    samples = PostgreSQLSamples(database)
    identifiers: set[UUID] = set()
    for _ in range(delivery_policy.batch * 2 + 1):
        sample = replace(
            private_sample,
            id=uuid4(),
            owner_id=uuid4(),
            expires_at=harness.clock.now(),
            object_key=f"samples/{uuid4()}/expired",
        )
        identifiers.add(sample.id)
        await samples.reserve(sample, 1)
        harness.storage.items[sample.object_key] = b"expired private payload"
    initial = harness.clock.now()
    cleanup_inference.failure = True
    await drain(cleanup_maintenance, asyncio.Event())
    assert harness.clock.now() == initial
    assert not harness.storage.items
    async with database() as session:
        assert not (await session.scalars(select(SampleRow))).all()
        cancellations = (
            await session.scalars(select(OutboxRow).where(OutboxRow.action == OutboxAction.CANCEL))
        ).all()
        assert {UUID(str(row.body["sample_id"])) for row in cancellations} == identifiers
        assert all(
            row.sample_id is None and set(row.body) == {"sample_id"} for row in cancellations
        )
    cleanup_inference.failure = False
    harness.clock.value += delivery_policy.initial_retry
    while (await cleanup_maintenance.deliver()).continue_immediately:
        pass
    assert set(cleanup_inference.cancelled_samples) == identifiers
    async with database() as session:
        assert not (await session.scalars(select(OutboxRow))).all()


async def test_failed_delivery_is_backed_off_and_stopped_drain_does_not_start_io(
    cleanup_maintenance: SearchMaintenance,
    cleanup_inference: CleanupCheckingInference,
    database: async_sessionmaker[AsyncSession],
    harness: SearchHarness,
) -> None:
    sample_id = uuid4()
    await PostgreSQLSamples(database).schedule_cancel(sample_id, harness.clock.now())
    await PostgreSQLSamples(database).schedule_cancel(sample_id, harness.clock.now())
    stop = asyncio.Event()
    stop.set()
    assert (await drain(cleanup_maintenance, stop))[0].completed == 0
    cleanup_inference.failure = True
    delivered, cleaned = await drain(cleanup_maintenance, asyncio.Event())
    assert delivered.failed == 1 and not delivered.continue_immediately
    assert cleaned.completed == 0
    assert (await cleanup_maintenance.deliver()).failed == 0
    async with database() as session:
        row = await session.get(OutboxRow, sample_id)
        assert row is not None and row.attempts == 1 and row.available_at > harness.clock.now()
