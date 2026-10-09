from dataclasses import replace
from datetime import datetime, timedelta
from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from memes_kernel.values import FileRef, Sha256
from memes_processing.application.dto.coordinator import (
    CallbackTarget,
    CoordinatorCode,
    CoordinatorError,
    EventDestination,
    MediaKind,
    Source,
    SourceType,
)
from memes_processing.application.use_cases.retry import RetryFeature
from memes_processing.application.use_cases.sources import PublishSource
from memes_processing.domain.features import FeatureKind
from memes_processing.infrastructure.http.delivery import RESTDelivery
from memes_processing.infrastructure.persistence.models import OutboxRow, SourceRow
from memes_processing.infrastructure.persistence.repository import PostgresRepository
from memes_processing_tests.fixtures import CoordinatorFixture


@pytest.fixture
def shadow_sample(now: datetime) -> Source:
    return Source(
        id=uuid4(),
        type=SourceType.SAMPLE,
        kind=MediaKind.IMAGE,
        file=FileRef(id=uuid4(), sha256=Sha256(value="a" * 64), size_bytes=128),
        object_key="private/source",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        callback_target=CallbackTarget.SHADOW,
    )


@pytest.mark.asyncio
async def test_private_shadow_callback_is_persisted_and_delivered_to_shadow_search(
    coordinator: CoordinatorFixture, shadow_sample: Source, now: datetime
) -> None:
    selected = replace(coordinator.policy, callback_targets=frozenset(CallbackTarget))
    await coordinator.transaction.run(
        PublishSource(event_id=uuid4(), source=shadow_sample, policy=selected)
    )
    async with coordinator.transaction.sessions() as session:
        repository = PostgresRepository(session)
        restored = await repository.source(shadow_sample.id)
        assert restored is not None
        assert restored.callback_target is CallbackTarget.SHADOW
        events = await repository.outgoing(now=now)
        assert events[0].destination is EventDestination.SHADOW_SAMPLE
        stored = (await session.scalars(select(OutboxRow))).all()
        assert all(item.destination == EventDestination.SHADOW_SAMPLE for item in stored)
        event = events[0]
    settings = coordinator.settings.model_copy(
        update={"shadow_search_url": "https://shadow-search.invalid"}
    )

    def delivered(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "shadow-search.invalid"
        assert request.url.path == "/internal/search/sample-features"
        assert request.headers["X-Internal-Key"] == settings.internal_api_key
        return httpx.Response(HTTPStatus.OK, json={"accepted": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(delivered)) as client:
        await RESTDelivery(client, settings).deliver(event)


@pytest.mark.asyncio
async def test_unconfigured_shadow_callback_is_rejected_before_source_acceptance(
    coordinator: CoordinatorFixture, shadow_sample: Source
) -> None:
    with pytest.raises(CoordinatorError) as rejected:
        await coordinator.transaction.run(
            PublishSource(
                event_id=uuid4(),
                source=shadow_sample,
                policy=coordinator.policy,
            )
        )
    assert rejected.value.code is CoordinatorCode.UNAVAILABLE
    async with coordinator.transaction.sessions() as session:
        assert await session.get(SourceRow, shadow_sample.id) is None


@pytest.mark.asyncio
async def test_private_source_replay_rejects_changed_identity_contract(
    coordinator: CoordinatorFixture, shadow_sample: Source
) -> None:
    pinned = coordinator.policy.versions[FeatureKind.VISUAL_EMBEDDING]
    sample = replace(
        shadow_sample, callback_target=CallbackTarget.PRIMARY, embedding_version=pinned
    )
    await coordinator.transaction.run(
        PublishSource(event_id=uuid4(), source=sample, policy=coordinator.policy)
    )
    conflicts = (
        replace(sample, embedding_version=replace(pinned, weights="incompatible-private-passport")),
        replace(sample, type=SourceType.QUERY),
        replace(sample, kind=MediaKind.AUDIO),
    )
    for candidate in conflicts:
        with pytest.raises(CoordinatorError) as rejected:
            await coordinator.transaction.run(
                PublishSource(event_id=uuid4(), source=candidate, policy=coordinator.policy)
            )
        assert rejected.value.code is CoordinatorCode.SOURCE_CONFLICT
    async with coordinator.transaction.sessions() as session:
        repository = PostgresRepository(session)
        assert await repository.source(sample.id) == sample
        assert len(await repository.source_tasks(sample.id)) == 3


@pytest.mark.asyncio
async def test_private_retry_preserves_pinned_passport_before_mutation(
    coordinator: CoordinatorFixture, shadow_sample: Source, now: datetime
) -> None:
    pinned = coordinator.policy.versions[FeatureKind.VISUAL_EMBEDDING]
    sample = replace(
        shadow_sample, callback_target=CallbackTarget.PRIMARY, embedding_version=pinned
    )
    await coordinator.transaction.run(
        PublishSource(event_id=uuid4(), source=sample, policy=coordinator.policy)
    )
    command = RetryFeature(
        source_id=sample.id,
        kind=FeatureKind.VISUAL_EMBEDDING,
        feature_version=replace(pinned, weights="incompatible-private-passport"),
        policy=coordinator.policy,
        now=now,
    )
    with pytest.raises(CoordinatorError) as rejected:
        await coordinator.transaction.run(command)
    assert rejected.value.code is CoordinatorCode.WRONG_VERSION
    async with coordinator.transaction.sessions() as session:
        repository = PostgresRepository(session)
        assert len(await repository.source_tasks(sample.id)) == 3
    accepted = await coordinator.transaction.run(replace(command, feature_version=pinned))
    assert accepted["generation"] == 2
    async with coordinator.transaction.sessions() as session:
        repository = PostgresRepository(session)
        tasks = await repository.source_tasks(sample.id)
        assert len(tasks) == 4
        assert all(
            work.task.input.version == pinned
            for work in tasks
            if work.task.input.kind is FeatureKind.VISUAL_EMBEDDING
        )
