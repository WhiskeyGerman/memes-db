from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

from memes_catalog.application.dto.catalog import Actor
from memes_catalog.infrastructure.metrics import QUERIES
from memes_catalog_tests.conftest import CatalogHarness
from memes_platform.application.dto.metrics import MetricName
from memes_platform.infrastructure.metrics import PostgreSQLMetrics


async def test_business_metrics_follow_publication_voting_merge_and_deletion(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    async def snapshot() -> dict[MetricName, float]:
        return {
            sample.name: sample.value
            for sample in await PostgreSQLMetrics(harness.factory.engine, QUERIES).snapshot()
        }

    initial = await snapshot()
    assert initial[MetricName.MEDIA_PUBLISHED] == 0
    assert initial[MetricName.GROUPS_ACTIVE] == 0
    assert initial[MetricName.BALLOTS_OPEN] == 0
    first = await harness.publish(actor, png(57))
    second = await harness.publish(actor, png(58))
    published = await snapshot()
    assert published[MetricName.MEDIA_PUBLISHED] == 2
    assert published[MetricName.GROUPS_ACTIVE] == 2

    harness.variations.merge_threshold = 2
    await harness.variations.merge(
        actor=actor,
        first_id=first.group.id,
        second_id=second.group.id,
        first_revision=first.group.composition_revision.value,
        second_revision=second.group.composition_revision.value,
        now=harness.now,
    )
    assert (await snapshot())[MetricName.BALLOTS_OPEN] == 1
    await harness.merge(first, second, replace(actor, user_id=uuid4()))
    merged = await snapshot()
    assert merged[MetricName.BALLOTS_OPEN] == 0
    assert merged[MetricName.GROUPS_ACTIVE] == 1
    assert merged[MetricName.MEDIA_PUBLISHED] == 2
    await harness.catalog.delete(media_id=first.media.id, actor=actor, now=harness.now)
    await harness.catalog.delete(media_id=second.media.id, actor=actor, now=harness.now)
    deleted = await snapshot()
    assert deleted[MetricName.MEDIA_PUBLISHED] == 0
    assert deleted[MetricName.GROUPS_ACTIVE] == 0


async def test_metrics_reflect_durable_queue_and_cleanup_without_content(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    view = await harness.publish(actor, png(55))
    await harness.catalog.delete(media_id=view.media.id, actor=actor, now=harness.now)
    samples = await PostgreSQLMetrics(harness.factory.engine, QUERIES).snapshot()
    values = {sample.name: sample.value for sample in samples}
    assert values[MetricName.OUTBOX_PENDING] >= 2
    assert values[MetricName.CLEANUP_PENDING] >= 1
    assert values[MetricName.STORAGE_BYTES] == 0


async def test_replay_deletions_and_reindex_are_idempotent(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    view = await harness.publish(actor, png(56))
    await harness.catalog.delete(media_id=view.media.id, actor=actor, now=harness.now)
    assert await harness.catalog.replay_deletions() == 1
    assert await harness.catalog.replay_deletions() == 1
    assert await harness.catalog.reindex_search(batch_size=1) == 1
