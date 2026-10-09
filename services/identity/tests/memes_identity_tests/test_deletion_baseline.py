from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.dto.outbox import DeletionDestination, DeletionRecipient
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.use_cases.deletion_baseline import SeedShadowSearchDeletions
from memes_identity.domain.user import UserId
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.deletion_baseline import SqlDeletedOwnerSource
from memes_identity.infrastructure.persistence.models import AccountDeliveryRow, AccountOutboxRow
from memes_identity.infrastructure.persistence.unit_of_work import SqlIdentityUnitOfWork
from memes_identity_tests.fakes import FixedClock, RecordingDeletionPublisher


@pytest.mark.asyncio
async def test_baseline_is_paged_shadow_only_and_safe_to_repeat_after_acceptance(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_publisher: RecordingDeletionPublisher,
    shadow_destination: DeletionDestination,
    deleted_owners: tuple[UserId, ...],
) -> None:
    baseline = SeedShadowSearchDeletions(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        source=SqlDeletedOwnerSource(sql_factory),
        publisher=deletion_publisher,
        clock=identity_clock,
        destination=shadow_destination,
    )
    assert await baseline.execute() == len(deleted_owners)
    assert await baseline.execute() == len(deleted_owners)
    assert len(deletion_publisher.calls) == len(deleted_owners)
    assert {call.event.user_id for call in deletion_publisher.calls} == set(deleted_owners)
    assert all(call.destination == shadow_destination for call in deletion_publisher.calls)
    async with sql_factory() as session:
        events = (await session.scalars(select(AccountOutboxRow))).all()
        deliveries = (await session.scalars(select(AccountDeliveryRow))).all()
        assert len(events) == len(deliveries) == len(deleted_owners)
        assert all(event.delivered_at is not None and not event.delete_media for event in events)
        assert all(row.recipient == DeletionRecipient.SHADOW_SEARCH for row in deliveries)


@pytest.mark.asyncio
async def test_unavailable_baseline_fails_gate_and_persisted_retry_survives_process_restart(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_publisher: RecordingDeletionPublisher,
    shadow_destination: DeletionDestination,
    deleted_owners: tuple[UserId, ...],
) -> None:
    deletion_publisher.rejected.add(DeletionRecipient.SHADOW_SEARCH)
    baseline = SeedShadowSearchDeletions(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        source=SqlDeletedOwnerSource(sql_factory),
        publisher=deletion_publisher,
        clock=identity_clock,
        destination=shadow_destination,
    )
    with pytest.raises(IdentityError) as blocked:
        await baseline.execute()
    assert blocked.value.code is IdentityFailure.BACKING_SERVICE_UNAVAILABLE
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as uow:
        assert await uow.outbox.pending(shadow_destination) == len(deleted_owners)
    deletion_publisher.rejected.clear()
    identity_clock.value += timedelta(seconds=identity_settings.outbox_retry_seconds)
    restarted = SeedShadowSearchDeletions(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        source=SqlDeletedOwnerSource(sql_factory),
        publisher=deletion_publisher,
        clock=identity_clock,
        destination=shadow_destination,
    )
    assert await restarted.execute() == len(deleted_owners)
    assert len(deletion_publisher.calls) == len(deleted_owners) * 2
