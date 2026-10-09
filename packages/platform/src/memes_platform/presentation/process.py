"""Ограниченное завершение отдельного фонового process type."""

import asyncio
from collections.abc import Awaitable
from contextlib import suppress


async def graceful_batch(
    operation: Awaitable[int], stopping: asyncio.Event, *, grace_seconds: int
) -> int:
    """Завершить текущую работу или отменить её после grace period.

    Args:
        operation: Идемпотентный ограниченный пакет.
        stopping: Сигнал остановки процесса.
        grace_seconds: Предел ожидания при SIGTERM.
    """
    work = asyncio.ensure_future(operation)
    signal = asyncio.create_task(stopping.wait())
    try:
        await asyncio.wait((work, signal), return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            return await work
        try:
            return await asyncio.wait_for(asyncio.shield(work), timeout=grace_seconds)
        except TimeoutError:
            work.cancel()
            try:
                await work
            except asyncio.CancelledError:
                return 0
            return 0
    finally:
        signal.cancel()
        with suppress(asyncio.CancelledError):
            await signal
        if not work.done():
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work
