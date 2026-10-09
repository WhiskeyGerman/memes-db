from collections.abc import Callable

from memes_catalog.application.dto.catalog import Actor
from memes_catalog.infrastructure.metrics import QUERIES
from memes_catalog_tests.conftest import CatalogHarness
from memes_platform.application.dto.metrics import MetricName
from memes_platform.infrastructure.metrics import PostgreSQLMetrics


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
