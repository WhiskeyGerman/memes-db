"""Независимые durable потоки основного и параллельного Search индексов."""

from collections.abc import Callable

import pytest
from sqlalchemy import func, select

from memes_catalog.application.dto.catalog import Actor, CatalogDefault, SearchIndexTarget
from memes_catalog.application.use_cases.delivery import DeliverEvents
from memes_catalog.infrastructure.persistence.events import DeliveryTarget
from memes_catalog.infrastructure.persistence.models import OutboxRow
from memes_catalog.infrastructure.persistence.outbox import PostgresOutbox
from memes_catalog_tests.conftest import CatalogHarness

pytestmark = pytest.mark.integration


async def test_shadow_failure_does_not_block_primary_delivery(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    harness.factory.mirror_search = True
    harness.delivery.unavailable_targets.add(DeliveryTarget.SHADOW_STATE)
    dispatcher = DeliverEvents(PostgresOutbox(harness.sessions), harness.delivery)
    first = await harness.publish(actor, png(61))
    await dispatcher.execute(now=harness.now, limit=CatalogDefault.OUTBOX_BATCH)
    second = await harness.publish(actor, png(62))
    await harness.merge(first, second, actor)
    while await dispatcher.execute(now=harness.now, limit=CatalogDefault.OUTBOX_BATCH):
        continue
    async with harness.sessions() as session:
        primary_pending = await session.scalar(
            select(func.count())
            .select_from(OutboxRow)
            .where(
                OutboxRow.target == DeliveryTarget.SEARCH_STATE,
                OutboxRow.delivered_at.is_(None),
            )
        )
        shadow_pending = await session.scalar(
            select(func.count())
            .select_from(OutboxRow)
            .where(
                OutboxRow.target == DeliveryTarget.SHADOW_STATE,
                OutboxRow.delivered_at.is_(None),
            )
        )
        assert primary_pending == 0
        assert shadow_pending is not None and shadow_pending >= 2
        assert (
            await session.scalar(
                select(OutboxRow.id).where(
                    OutboxRow.target == DeliveryTarget.SHADOW_STATE,
                    OutboxRow.payload["revision"].as_integer() == 2,
                )
            )
            is not None
        )


async def test_shadow_baseline_and_deletion_preserve_both_index_streams(
    harness: CatalogHarness, actor: Actor, png: Callable[[int], bytes]
) -> None:
    harness.factory.mirror_search = True
    view = await harness.publish(actor, png(63))
    async with harness.sessions() as session:
        before = set(await session.scalars(select(OutboxRow.id)))
    await harness.catalog.reindex_search(batch_size=1, target=SearchIndexTarget.SHADOW)
    async with harness.sessions() as session:
        baseline = tuple(await session.scalars(select(OutboxRow).where(~OutboxRow.id.in_(before))))
        assert len(baseline) == 1 and baseline[0].target == DeliveryTarget.SHADOW_STATE
        assert baseline[0].payload["media_id"] == str(view.media.id)
    await harness.catalog.delete(media_id=view.media.id, actor=actor, now=harness.now)
    async with harness.sessions() as session:
        tombstones = tuple(
            await session.scalars(
                select(OutboxRow).where(
                    OutboxRow.target.in_(
                        (DeliveryTarget.SEARCH_STATE, DeliveryTarget.SHADOW_STATE)
                    ),
                    OutboxRow.payload["active"].as_boolean().is_(False),
                )
            )
        )
        assert {event.target for event in tombstones} == {
            DeliveryTarget.SEARCH_STATE,
            DeliveryTarget.SHADOW_STATE,
        }
        assert len(tombstones) == 2
        assert len({event.id for event in tombstones}) == 2
