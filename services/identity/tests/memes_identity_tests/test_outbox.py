import asyncio
from datetime import timedelta
from http import HTTPStatus

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.dto.authentication import AccountDeleted
from memes_identity.application.dto.outbox import (
    AccountDeletionDelivery,
    DeletionDestination,
    DeletionRecipient,
)
from memes_identity.application.use_cases.outbox import DeliverAccountDeletion
from memes_identity.infrastructure.clients.account_deletion import RestAccountDeletionPublisher
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.models import AccountDeliveryRow, AccountOutboxRow
from memes_identity.infrastructure.persistence.unit_of_work import SqlIdentityUnitOfWork
from memes_identity_tests.fakes import FixedClock, RecordingDeletionPublisher


@pytest.mark.asyncio
async def test_shadow_receives_only_technical_deletion_and_internal_authentication(
    identity_settings: IdentitySettings,
    deletion_event: AccountDeleted,
    shadow_destination: DeletionDestination,
) -> None:
    requests: list[httpx.Request] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(HTTPStatus.NO_CONTENT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
        publisher = RestAccountDeletionPublisher(client, identity_settings)
        assert await publisher.publish(
            AccountDeletionDelivery(
                delivery_id=deletion_event.event_id,
                event=deletion_event,
                destination=shadow_destination,
                claim_token=deletion_event.event_id,
            )
        )
    assert len(requests) == 1
    assert requests[0].url.host == "shadow"
    assert str(deletion_event.user_id) in requests[0].url.path
    assert requests[0].headers["X-Internal-Key"] == identity_settings.internal_api_key
    assert b"delete_media" not in requests[0].content
    assert b"user_id" not in requests[0].content


@pytest.mark.asyncio
async def test_shadow_failure_does_not_repeat_confirmed_primary_deliveries(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_event: AccountDeleted,
    deletion_publisher: RecordingDeletionPublisher,
    shadow_destination: DeletionDestination,
) -> None:
    settings = identity_settings.model_copy(
        update={"shadow_search_url": shadow_destination.base_url}
    )
    async with SqlIdentityUnitOfWork(sql_factory, settings) as seed:
        await seed.outbox.add(deletion_event)
        await seed.commit()
    deletion_publisher.rejected.add(DeletionRecipient.SHADOW_SEARCH)
    deliver = DeliverAccountDeletion(
        uow=SqlIdentityUnitOfWork(sql_factory, settings),
        publisher=deletion_publisher,
        clock=identity_clock,
    )
    assert all([await deliver.execute() for _ in range(3)])
    assert not await deliver.execute()
    async with sql_factory() as session:
        rows = (await session.scalars(select(AccountDeliveryRow))).all()
        assert len(rows) == 3
        assert {row.recipient for row in rows if row.delivered_at is not None} == {
            DeletionRecipient.CATALOG,
            DeletionRecipient.SEARCH,
        }
        parent = await session.get(AccountOutboxRow, deletion_event.event_id)
        assert parent is not None and parent.delivered_at is None
    deletion_publisher.rejected.clear()
    identity_clock.value += timedelta(seconds=settings.outbox_retry_seconds)
    assert await deliver.execute()
    assert not await deliver.execute()
    assert [call.destination.recipient for call in deletion_publisher.calls].count(
        DeletionRecipient.CATALOG
    ) == 1
    assert [call.destination.recipient for call in deletion_publisher.calls].count(
        DeletionRecipient.SEARCH
    ) == 1
    async with sql_factory() as session:
        parent = await session.get(AccountOutboxRow, deletion_event.event_id)
        assert parent is not None and parent.delivered_at == identity_clock.now()


@pytest.mark.asyncio
async def test_http_runs_after_claim_commit_without_database_row_lock(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_event: AccountDeleted,
    deletion_publisher: RecordingDeletionPublisher,
    shadow_destination: DeletionDestination,
) -> None:
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as seed:
        await seed.outbox.add(deletion_event, destination=shadow_destination)
        await seed.commit()
    deletion_publisher.pause = True
    delivery = DeliverAccountDeletion(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        publisher=deletion_publisher,
        clock=identity_clock,
    )
    pending = asyncio.create_task(delivery.execute())
    try:
        await asyncio.wait_for(
            deletion_publisher.started.wait(), timeout=identity_settings.http_timeout_seconds
        )
        async with sql_factory() as session:
            row = await session.scalar(select(AccountDeliveryRow).with_for_update(nowait=True))
            assert row is not None and row.claim_token is not None and row.lease_until is not None
    finally:
        deletion_publisher.resume.set()
    assert await pending


@pytest.mark.asyncio
async def test_expired_lease_can_be_reclaimed_and_old_worker_cannot_ack_or_retry(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_event: AccountDeleted,
    shadow_destination: DeletionDestination,
) -> None:
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as seed:
        await seed.outbox.add(deletion_event, destination=shadow_destination)
        await seed.commit()
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as first:
        old = await first.outbox.claim(identity_clock.now())
        assert old is not None
        await first.commit()
    identity_clock.value += timedelta(seconds=identity_settings.outbox_lease_seconds)
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as second:
        replacement = await second.outbox.claim(identity_clock.now())
        assert replacement is not None and replacement.claim_token != old.claim_token
        await second.commit()
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as stale:
        await stale.outbox.acknowledge(old, identity_clock.now())
        await stale.outbox.postpone(old, identity_clock.now())
        await stale.commit()
    async with sql_factory() as session:
        row = await session.get(AccountDeliveryRow, old.delivery_id)
        assert row is not None and row.delivered_at is None
        assert row.attempts == 0 and row.claim_token == replacement.claim_token
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as valid:
        await valid.outbox.acknowledge(replacement, identity_clock.now())
        await valid.commit()
    async with sql_factory() as session:
        parent = await session.get(AccountOutboxRow, deletion_event.event_id)
        assert parent is not None and parent.delivered_at is not None


@pytest.mark.asyncio
async def test_legacy_undelivered_events_expand_into_configured_recipient_deliveries(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_event: AccountDeleted,
) -> None:
    async with sql_factory.begin() as session:
        session.add(
            AccountOutboxRow(
                event_id=deletion_event.event_id,
                user_id=deletion_event.user_id,
                delete_media=deletion_event.delete_media,
                occurred_at=deletion_event.occurred_at,
                next_attempt_at=deletion_event.occurred_at,
                attempts=0,
                delivered_at=None,
            )
        )
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as claim:
        delivery = await claim.outbox.claim(identity_clock.now())
        assert delivery is not None and delivery.event == deletion_event
        await claim.commit()
    async with sql_factory() as session:
        assert len((await session.scalars(select(AccountDeliveryRow))).all()) == 2


@pytest.mark.asyncio
async def test_pending_delivery_preserves_destination_when_release_configuration_changes(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_event: AccountDeleted,
    shadow_destination: DeletionDestination,
) -> None:
    configured = identity_settings.model_copy(
        update={"shadow_search_url": shadow_destination.base_url}
    )
    async with SqlIdentityUnitOfWork(sql_factory, configured) as seed:
        await seed.outbox.add(deletion_event)
        await seed.commit()
    changed = configured.model_copy(update={"shadow_search_url": "http://next-shadow"})
    async with SqlIdentityUnitOfWork(sql_factory, changed) as claim:
        delivery = await claim.outbox.claim(identity_clock.now(), destination=shadow_destination)
        assert delivery is not None and delivery.destination == shadow_destination
        await claim.commit()


@pytest.mark.asyncio
async def test_new_shadow_recipient_reopens_parent_and_cannot_be_removed_by_retention(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_event: AccountDeleted,
    deletion_publisher: RecordingDeletionPublisher,
    shadow_destination: DeletionDestination,
) -> None:
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as seed:
        await seed.outbox.add(deletion_event)
        await seed.commit()
    deliver = DeliverAccountDeletion(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        publisher=deletion_publisher,
        clock=identity_clock,
    )
    while await deliver.execute():
        pass
    changed = identity_settings.model_copy(
        update={"shadow_search_url": shadow_destination.base_url}
    )
    async with SqlIdentityUnitOfWork(sql_factory, changed) as seed:
        await seed.outbox.add(deletion_event)
        await seed.commit()
    async with sql_factory() as session:
        parent = await session.get(AccountOutboxRow, deletion_event.event_id)
        assert parent is not None and parent.delivered_at is None
        assert len((await session.scalars(select(AccountDeliveryRow))).all()) == 3
    async with SqlIdentityUnitOfWork(sql_factory, changed) as claim:
        assert await claim.outbox.pending(shadow_destination) == 1
