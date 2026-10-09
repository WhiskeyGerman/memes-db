from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from memes_worker.application.dto.runtime import WorkResult
from memes_worker.infrastructure.cache.sqlite import SQLiteCache


@pytest.mark.asyncio
async def test_cache_ttl_never_extends_on_read_or_duplicate_write(
    cache: SQLiteCache, result: WorkResult, now: datetime
) -> None:
    deadline = now + timedelta(minutes=5)
    await cache.put(result, expires_at=deadline)
    assert await cache.get(result.key, now=deadline - timedelta(seconds=1)) == result
    await cache.put(result, expires_at=deadline + timedelta(minutes=5))
    assert await cache.get(result.key, now=deadline) is None
    await cache.purge(now=deadline)
    assert await cache.get(result.key, now=deadline - timedelta(seconds=1)) is None


@pytest.mark.asyncio
async def test_same_hash_different_source_never_shares_result(
    cache: SQLiteCache, result: WorkResult, now: datetime
) -> None:
    await cache.put(result, expires_at=now + timedelta(minutes=5))
    assert await cache.get(replace(result.key, source_id=uuid4()), now=now) is None


@pytest.mark.asyncio
async def test_source_revocation_removes_all_cycles(
    cache: SQLiteCache, result: WorkResult, now: datetime
) -> None:
    await cache.put(result, expires_at=now + timedelta(minutes=5))
    await cache.put(
        replace(result, key=replace(result.key, cycle_id=uuid4())),
        expires_at=now + timedelta(minutes=5),
    )
    await cache.revoke(result.key.source_id)
    assert await cache.get(result.key, now=now) is None
