import asyncio

from memes_processing.presentation.coordinator import deliver_batch
from memes_processing_tests.fixtures import MemoryArtifacts, MemoryDelivery, QueuedTransaction


async def test_ready_backlog_drains_without_idle_timer_between_full_passes(
    queued_transaction: QueuedTransaction,
    memory_artifacts: MemoryArtifacts,
    memory_delivery: MemoryDelivery,
) -> None:
    queued_transaction.remaining = 321
    stopped = asyncio.Event()
    passes = 0
    while True:
        batch = await deliver_batch(
            transaction=queued_transaction,
            storage=memory_artifacts,
            delivery=memory_delivery,
            stopped=stopped,
        )
        passes += 1
        if not batch.should_continue:
            break
    assert passes == 4
    assert queued_transaction.delivered == 321
    assert queued_transaction.remaining == 0


async def test_unavailable_recipient_does_not_trigger_immediate_unbounded_retry(
    queued_transaction: QueuedTransaction,
    memory_artifacts: MemoryArtifacts,
    memory_delivery: MemoryDelivery,
) -> None:
    queued_transaction.remaining = 321
    queued_transaction.failed = True
    batch = await deliver_batch(
        transaction=queued_transaction,
        storage=memory_artifacts,
        delivery=memory_delivery,
        stopped=asyncio.Event(),
    )
    assert batch.completed == 0
    assert not batch.should_continue
    assert queued_transaction.remaining > 0


async def test_stop_between_operations_leaves_outbox_for_next_process(
    queued_transaction: QueuedTransaction,
    memory_artifacts: MemoryArtifacts,
    memory_delivery: MemoryDelivery,
) -> None:
    stopped = asyncio.Event()
    queued_transaction.remaining = 321
    queued_transaction.stop_on_cleanup = stopped
    batch = await deliver_batch(
        transaction=queued_transaction,
        storage=memory_artifacts,
        delivery=memory_delivery,
        stopped=stopped,
    )
    assert batch.completed == 0
    assert queued_transaction.remaining == 321
    assert queued_transaction.cleanup_calls == 1
    assert not batch.should_continue
