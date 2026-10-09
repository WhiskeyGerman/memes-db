import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http import HTTPStatus
from typing import TypedDict
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import BaseModel
from sqlalchemy import Connection, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.samples import SampleState
from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.use_cases.sample_admission import (
    AdmissionLimit,
    SharedSampleAdmission,
)
from memes_search.infrastructure.migrations.versions.v0004_shared_sample_admission import upgrade
from memes_search.infrastructure.persistence.models import DeletedOwnerRow, SampleRow
from memes_search.infrastructure.persistence.sample_admission import (
    AdmissionBatch,
    PostgreSQLSampleAdmission,
)
from memes_search.infrastructure.persistence.sample_admission_models import SampleAdmissionRow
from memes_search.infrastructure.sample_admission import AdmissionPath, RestSampleAdmission
from memes_search_tests.fakes import Clock


class AdmissionFixture(StrEnum):
    BASE_URL = "http://primary.invalid"
    KEY = "admission-fixture-key-for-internal-requests"


class ReservationBody(BaseModel):
    sample_id: UUID
    owner_id: UUID
    created_at: datetime
    expires_at: datetime


class ReservationValues(TypedDict):
    owner_id: UUID
    created_at: datetime
    expires_at: datetime


def create_admission_table(connection: Connection) -> None:
    table = SampleAdmissionRow.__table__
    assert isinstance(table, Table)
    table.create(connection, checkfirst=True)


def drop_admission_table(connection: Connection) -> None:
    table = SampleAdmissionRow.__table__
    assert isinstance(table, Table)
    table.drop(connection)


@pytest.fixture
async def admission_factory(
    database: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    async with database.begin() as session:
        connection = await session.connection()
        await connection.run_sync(create_admission_table)
    return database


@pytest.fixture
def admission_clock() -> Clock:
    return Clock(value=datetime.now(UTC))


@pytest.fixture
def admission_policy() -> SearchSessionPolicy:
    return SearchSessionPolicy()


@pytest.fixture
def admission_owner() -> UUID:
    return uuid4()


@pytest.fixture
def primary_admission(
    admission_factory: async_sessionmaker[AsyncSession],
    admission_clock: Clock,
    admission_policy: SearchSessionPolicy,
) -> SharedSampleAdmission:
    return SharedSampleAdmission(
        repository=PostgreSQLSampleAdmission(admission_factory),
        clock=admission_clock,
        policy=admission_policy,
    )


@pytest.fixture
async def shadow_admission(
    primary_admission: SharedSampleAdmission,
) -> AsyncIterator[RestSampleAdmission]:
    async def receive(request: httpx.Request) -> httpx.Response:
        assert request.headers[AdmissionPath.INTERNAL_KEY] == AdmissionFixture.KEY
        try:
            if request.method == "POST":
                body = ReservationBody.model_validate_json(request.content)
                await primary_admission.reserve(**body.model_dump())
            elif request.method == "DELETE":
                await primary_admission.release(UUID(request.url.path.rsplit("/", 1)[-1]))
            else:
                return httpx.Response(HTTPStatus.METHOD_NOT_ALLOWED)
        except SearchError as error:
            statuses = {
                SearchErrorCode.SAMPLE_LIMIT: HTTPStatus.TOO_MANY_REQUESTS,
                SearchErrorCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
                SearchErrorCode.INVALID_SAMPLE: HTTPStatus.UNPROCESSABLE_ENTITY,
            }
            return httpx.Response(statuses.get(error.code, HTTPStatus.SERVICE_UNAVAILABLE))
        return httpx.Response(HTTPStatus.NO_CONTENT)

    client = RestSampleAdmission(
        base_url=AdmissionFixture.BASE_URL,
        internal_api_key=AdmissionFixture.KEY,
        timeout=15,
        transport=httpx.MockTransport(receive),
    )
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_primary_and_shadow_race_shares_exactly_two_owner_slots(
    primary_admission: SharedSampleAdmission,
    shadow_admission: RestSampleAdmission,
    admission_factory: async_sessionmaker[AsyncSession],
    admission_owner: UUID,
    admission_clock: Clock,
    admission_policy: SearchSessionPolicy,
) -> None:
    third_process = SharedSampleAdmission(
        repository=PostgreSQLSampleAdmission(admission_factory),
        clock=admission_clock,
        policy=admission_policy,
    )
    results = await asyncio.gather(
        *(
            process.reserve(
                sample_id=uuid4(),
                owner_id=admission_owner,
                created_at=admission_clock.now(),
                expires_at=admission_clock.now() + admission_policy.lifetime,
            )
            for process in (primary_admission, shadow_admission, third_process)
        ),
        return_exceptions=True,
    )
    assert results.count(None) == 2
    rejected = [result for result in results if isinstance(result, SearchError)]
    assert len(rejected) == 1 and rejected[0].code is SearchErrorCode.SAMPLE_LIMIT
    async with admission_factory() as session:
        assert await session.scalar(select(func.count()).select_from(SampleAdmissionRow)) == 2


@pytest.mark.asyncio
async def test_shadow_terminal_release_is_idempotent_and_returns_shared_slot(
    primary_admission: SharedSampleAdmission,
    shadow_admission: RestSampleAdmission,
    admission_owner: UUID,
    admission_clock: Clock,
    admission_policy: SearchSessionPolicy,
) -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    values: ReservationValues = {
        "owner_id": admission_owner,
        "created_at": admission_clock.now(),
        "expires_at": admission_clock.now() + admission_policy.lifetime,
    }
    await primary_admission.reserve(sample_id=first, **values)
    await shadow_admission.reserve(sample_id=second, **values)
    with pytest.raises(SearchError) as blocked:
        await shadow_admission.reserve(sample_id=third, **values)
    assert blocked.value.code is SearchErrorCode.SAMPLE_LIMIT
    await shadow_admission.release(second)
    await shadow_admission.release(second)
    await primary_admission.reserve(sample_id=third, **values)


@pytest.mark.asyncio
async def test_retry_does_not_extend_slot_and_owner_fence_prevents_late_reservation(
    primary_admission: SharedSampleAdmission,
    admission_factory: async_sessionmaker[AsyncSession],
    admission_owner: UUID,
    admission_clock: Clock,
    admission_policy: SearchSessionPolicy,
) -> None:
    identifier = uuid4()
    values: ReservationValues = {
        "owner_id": admission_owner,
        "created_at": admission_clock.now(),
        "expires_at": admission_clock.now() + admission_policy.lifetime,
    }
    await primary_admission.reserve(sample_id=identifier, **values)
    await primary_admission.reserve(sample_id=identifier, **values)
    altered: ReservationValues = {
        **values,
        "created_at": admission_clock.now() + timedelta(seconds=1),
        "expires_at": values["expires_at"] + timedelta(seconds=1),
    }
    with pytest.raises(SearchError) as modified:
        await primary_admission.reserve(sample_id=identifier, **altered)
    assert modified.value.code is SearchErrorCode.INVALID_SAMPLE
    await primary_admission.erase_owner(admission_owner)
    with pytest.raises(SearchError) as deleted:
        await primary_admission.reserve(sample_id=uuid4(), **values)
    assert deleted.value.code is SearchErrorCode.FORBIDDEN
    async with admission_factory() as session:
        assert await session.get(DeletedOwnerRow, admission_owner) is not None
        assert await session.scalar(select(func.count()).select_from(SampleAdmissionRow)) == 0


@pytest.mark.asyncio
async def test_server_rejects_excessive_or_future_ttl_and_expired_slots_do_not_count(
    primary_admission: SharedSampleAdmission,
    admission_factory: async_sessionmaker[AsyncSession],
    admission_owner: UUID,
    admission_clock: Clock,
    admission_policy: SearchSessionPolicy,
) -> None:
    for created, expired in (
        (
            admission_clock.now(),
            admission_clock.now() + admission_policy.lifetime + timedelta(seconds=1),
        ),
        (
            admission_clock.now() + timedelta(seconds=AdmissionLimit.CLOCK_SKEW_SECONDS + 1),
            admission_clock.now() + admission_policy.lifetime,
        ),
    ):
        with pytest.raises(SearchError) as invalid:
            await primary_admission.reserve(
                sample_id=uuid4(), owner_id=admission_owner, created_at=created, expires_at=expired
            )
        assert invalid.value.code is SearchErrorCode.INVALID_SAMPLE
    for _ in range(admission_policy.active_samples_per_user):
        await primary_admission.reserve(
            sample_id=uuid4(),
            owner_id=admission_owner,
            created_at=admission_clock.now(),
            expires_at=admission_clock.now() + admission_policy.lifetime,
        )
    admission_clock.value += admission_policy.lifetime
    await primary_admission.reserve(
        sample_id=uuid4(),
        owner_id=admission_owner,
        created_at=admission_clock.now(),
        expires_at=admission_clock.now() + admission_policy.lifetime,
    )
    await primary_admission.purge(admission_clock.now())
    async with admission_factory() as session:
        assert await session.scalar(select(func.count()).select_from(SampleAdmissionRow)) == 1


@pytest.mark.asyncio
async def test_transport_failure_is_fail_closed_without_a_private_upload_slot(
    admission_clock: Clock,
    admission_owner: UUID,
    admission_policy: SearchSessionPolicy,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("invalid admission server response", request=request)

    client = RestSampleAdmission(
        base_url=AdmissionFixture.BASE_URL,
        internal_api_key=AdmissionFixture.KEY,
        timeout=15,
        transport=httpx.MockTransport(fail),
    )
    try:
        with pytest.raises(SearchError) as rejected:
            await client.reserve(
                sample_id=uuid4(),
                owner_id=admission_owner,
                created_at=admission_clock.now(),
                expires_at=admission_clock.now() + admission_policy.lifetime,
            )
        assert rejected.value.code is SearchErrorCode.UNAVAILABLE
    finally:
        await client.close()


def apply_admission_migration(connection: Connection) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        upgrade()


@pytest.mark.asyncio
async def test_migration_backfills_only_alive_pending_primary_samples(
    admission_factory: async_sessionmaker[AsyncSession],
    admission_clock: Clock,
) -> None:
    expected: set[UUID] = set()
    async with admission_factory.begin() as session:
        connection = await session.connection()
        await connection.run_sync(drop_admission_table)
        for state in SampleState:
            identifier = uuid4()
            expires = admission_clock.now() + timedelta(minutes=5)
            session.add(
                SampleRow(
                    id=identifier,
                    owner_id=uuid4(),
                    state=state,
                    expires_at=expires,
                    body={"created_at": admission_clock.now().isoformat()},
                )
            )
            if state in {SampleState.UPLOADING, SampleState.QUEUED}:
                expected.add(identifier)
        session.add(
            SampleRow(
                id=uuid4(),
                owner_id=uuid4(),
                state=SampleState.QUEUED,
                expires_at=admission_clock.now() - timedelta(seconds=1),
                body={"created_at": (admission_clock.now() - timedelta(minutes=5)).isoformat()},
            )
        )
        await session.flush()
        await connection.run_sync(apply_admission_migration)
    async with admission_factory() as session:
        assert set((await session.scalars(select(SampleAdmissionRow.sample_id))).all()) == expected


async def test_expired_ledger_gc_is_bounded_and_can_resume(
    admission_factory: async_sessionmaker[AsyncSession],
    primary_admission: SharedSampleAdmission,
    admission_clock: Clock,
) -> None:
    async with admission_factory.begin() as session:
        session.add_all(
            SampleAdmissionRow(
                sample_id=uuid4(),
                owner_id=uuid4(),
                created_at=admission_clock.now() - timedelta(minutes=2),
                expires_at=admission_clock.now() - timedelta(minutes=1),
            )
            for _ in range(AdmissionBatch.PURGE + 1)
        )
    await primary_admission.purge(admission_clock.now())
    async with admission_factory() as session:
        assert await session.scalar(select(func.count()).select_from(SampleAdmissionRow)) == 1
    await primary_admission.purge(admission_clock.now())
    async with admission_factory() as session:
        assert await session.scalar(select(func.count()).select_from(SampleAdmissionRow)) == 0
