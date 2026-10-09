"""Регрессии смены поколения и удаления при выполняющемся inference."""

import asyncio
from collections.abc import AsyncIterable
from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_kernel.values import Revision
from memes_search.application.dto.delivery import OutboxAction
from memes_search.application.dto.execution import SearchRequest
from memes_search.application.dto.projections import ProjectionState
from memes_search.application.dto.queries import SearchMode
from memes_search.application.dto.samples import (
    PrivateSample,
    QueryInference,
    SampleFeatureChange,
    SampleState,
)
from memes_search.application.dto.sessions import GroupSnapshot, SearchSession, SearchSessionPolicy
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.use_cases.samples import SampleService
from memes_search.application.use_cases.search import SearchService
from memes_search.domain.ranking import RankedGroup, RankingPolicy
from memes_search.infrastructure.persistence.models import (
    DeletedOwnerRow,
    OutboxRow,
    RequestRow,
    SessionRow,
)
from memes_search.infrastructure.persistence.private_data import PostgreSQLPrivateData
from memes_search.infrastructure.persistence.samples import PostgreSQLSamples
from memes_search.infrastructure.persistence.sessions import PostgreSQLRequests, PostgreSQLSessions
from memes_search_tests.fakes import Inference, PausingStorage
from memes_search_tests.harness import SearchHarness

pytestmark = pytest.mark.integration


class PausingInference(Inference):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def query(
        self, request_id: UUID, text: str, space_id: str, owner_id: UUID | None
    ) -> QueryInference:
        self.entered.set()
        await self.release.wait()
        return await self.query_result(self.task_id)


async def test_sample_pending_terminal_preserves_cycle_and_late_ready(
    database: async_sessionmaker[AsyncSession],
    private_sample: PrivateSample,
    now: datetime,
) -> None:
    repository = PostgreSQLSamples(database)
    await repository.reserve(private_sample, 2)
    pending = SampleFeatureChange(
        event_id=uuid4(),
        sample_id=private_sample.id,
        kind="transcript",
        generation=1,
        state=ProjectionState.PENDING,
        cycle_id=uuid4(),
        version_key="fixture-version",
    )
    assert await repository.apply_features(pending, now)
    terminal = replace(pending, event_id=uuid4(), state=ProjectionState.EMPTY)
    assert not await repository.apply_features(
        replace(terminal, event_id=uuid4(), cycle_id=uuid4()), now
    )
    assert await repository.apply_features(terminal, now)
    assert not await repository.apply_features(replace(pending, event_id=uuid4()), now)
    assert not await repository.apply_features(replace(terminal, event_id=uuid4()), now)
    assert await repository.apply_features(
        replace(terminal, event_id=uuid4(), sample_state=SampleState.READY), now
    )
    ready = await repository.get(private_sample.id)
    assert ready is not None and ready.state is SampleState.READY
    assert not await repository.apply_features(
        replace(terminal, event_id=uuid4(), sample_state=SampleState.FAILED), now
    )
    ready = await repository.get(private_sample.id)
    assert ready is not None and ready.state is SampleState.READY


async def test_deleted_owner_rejects_every_late_private_write(
    database: async_sessionmaker[AsyncSession],
    private_sample: PrivateSample,
    private_request: SearchRequest,
    now: datetime,
) -> None:
    samples, requests, sessions = (
        PostgreSQLSamples(database),
        PostgreSQLRequests(database),
        PostgreSQLSessions(database),
    )
    group_id = uuid4()
    snapshot = SearchSession(
        id=uuid4(),
        query=private_request.query,
        created_at=now,
        expires_at=private_request.expires_at,
        results=(RankedGroup(group_id=group_id, representative_id=uuid4(), score=1.0),),
        groups=(GroupSnapshot(group_id=group_id, composition_revision=Revision()),),
    )
    await samples.reserve(private_sample, 2)
    await requests.save(private_request)
    await sessions.save(snapshot)
    eraser = PostgreSQLPrivateData(database)
    await eraser.erase_owner(private_sample.owner_id, now)
    await eraser.erase_owner(private_sample.owner_id, now)
    with pytest.raises(SearchError) as reserve_error:
        await samples.reserve(replace(private_sample, id=uuid4()), 2)
    with pytest.raises(SearchError) as sample_error:
        await samples.save(private_sample, enqueue=True)
    with pytest.raises(SearchError) as request_error:
        await requests.save(private_request)
    with pytest.raises(SearchError) as session_error:
        await sessions.save(snapshot)
    assert {
        item.value.code for item in (reserve_error, sample_error, request_error, session_error)
    } == {SearchErrorCode.FORBIDDEN}
    assert await requests.get(private_request.id) is None
    assert await sessions.get(snapshot.id) is None
    async with database() as transaction:
        assert await transaction.get(DeletedOwnerRow, private_sample.owner_id) is not None
        cancellation = await transaction.scalar(
            select(OutboxRow).where(OutboxRow.action == OutboxAction.CANCEL_QUERY)
        )
        assert cancellation is not None and cancellation.task_id == private_request.id


async def test_delete_during_inference_cancels_by_stable_request_and_cannot_recreate_snapshot(
    database: async_sessionmaker[AsyncSession],
    private_request: SearchRequest,
    harness: SearchHarness,
    now: datetime,
) -> None:
    inference = PausingInference()
    requests, sessions = PostgreSQLRequests(database), PostgreSQLSessions(database)
    service = SearchService(
        index=harness.index,
        sessions=sessions,
        requests=requests,
        catalog=harness.catalog,
        inference=inference,
        samples=harness.samples,
        clock=harness.clock,
        space_id="fixture-space",
        ranking=RankingPolicy(),
        session_policy=SearchSessionPolicy(),
    )
    query = replace(private_request.query, mode=SearchMode.SEMANTIC)
    running = asyncio.create_task(service.execute(query, private_request.id))
    await asyncio.wait_for(inference.entered.wait(), 5)
    assert query.owner_id is not None
    await PostgreSQLPrivateData(database).erase_owner(query.owner_id, now)
    async with database() as transaction:
        cancellation = await transaction.scalar(
            select(OutboxRow).where(OutboxRow.action == OutboxAction.CANCEL_QUERY)
        )
        assert cancellation is not None and cancellation.task_id == private_request.id
    inference.release.set()
    with pytest.raises(SearchError) as error:
        await running
    assert error.value.code is SearchErrorCode.FORBIDDEN
    async with database() as transaction:
        assert not (await transaction.scalars(select(RequestRow))).all()
        assert not (await transaction.scalars(select(SessionRow))).all()


async def private_chunks() -> AsyncIterable[bytes]:
    yield b"private upload that finishes after deletion"


async def test_late_sample_upload_cannot_recreate_owner_data_and_keeps_durable_file_cleanup(
    database: async_sessionmaker[AsyncSession],
    harness: SearchHarness,
    now: datetime,
) -> None:
    owner_id = uuid4()
    samples = PostgreSQLSamples(database)
    storage = PausingStorage()
    service = SampleService(
        repository=samples,
        admission=harness.admission,
        storage=storage,
        inspector=harness.catalog,
        catalog=harness.catalog,
        clock=harness.clock,
        policy=SearchSessionPolicy(),
        index=harness.index,
        embedding_version=harness.version,
    )
    uploading = asyncio.create_task(service.upload(owner_id, private_chunks()))
    await asyncio.wait_for(storage.entered.wait(), 5)
    await PostgreSQLPrivateData(database).erase_owner(owner_id, now)
    for sample in await samples.expired(now, 10):
        await samples.remove(sample.id)
    storage.release.set()
    with pytest.raises(SearchError) as error:
        await uploading
    assert error.value.code is SearchErrorCode.FORBIDDEN
    assert not storage.items
    async with database() as transaction:
        cleanups = (
            await transaction.scalars(
                select(OutboxRow).where(OutboxRow.action == OutboxAction.DELETE_OBJECT)
            )
        ).all()
        assert cleanups and any(message.available_at == now for message in cleanups)
