from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from memes_processing.infrastructure.persistence.models import OutboxRow
from memes_processing_tests.fixtures import CoordinatorFixture
from memes_worker.application.use_cases.runner import WorkerRunner
from memes_worker.infrastructure.cache.sqlite import SQLiteCache
from memes_worker.infrastructure.config.settings import WorkerSettings
from memes_worker.infrastructure.http.client import RESTCoordinatorClient
from memes_worker.infrastructure.ml.executor import MediaExecutor


@pytest.mark.asyncio
async def test_real_worker_preview_round_trip_and_deletion_revocation(
    processing_http: httpx.AsyncClient,
    coordinator: CoordinatorFixture,
    jpeg_preview: bytes,
    tmp_path: Path,
) -> None:
    internal = {"X-Internal-Key": coordinator.settings.internal_api_key}
    registered = await processing_http.post(
        "/v1/workers",
        json={"name": "preview-worker"},
        headers={"Authorization": f"Bearer {uuid4().hex}"},
    )
    registered.raise_for_status()
    credentials = registered.json()
    media_id, file_id = uuid4(), uuid4()
    key = f"originals/{media_id}/{file_id}"
    coordinator.storage.objects[key] = jpeg_preview
    published = await processing_http.post(
        "/internal/media/published",
        headers=internal,
        json={
            "event_id": str(uuid4()),
            "media_id": str(media_id),
            "file_id": str(file_id),
            "sha256": sha256(jpeg_preview).hexdigest(),
            "size_bytes": len(jpeg_preview),
            "media_kind": "image",
            "metadata": {},
            "occurred_at": datetime.now(UTC).isoformat(),
            "source_revision": 1,
            "object_key": key,
        },
    )
    published.raise_for_status()
    settings = WorkerSettings(
        coordinator_url="https://processing.invalid",
        worker_id=credentials["worker_id"],
        token=credentials["token"],
        release_id="integration",
        cache_directory=tmp_path / "worker",
    )
    cache = SQLiteCache(settings.cache_directory, maximum_bytes=settings.cache_bytes)
    client = RESTCoordinatorClient(processing_http, settings)
    executor = MediaExecutor(processing_http, settings)
    try:
        work = await client.poll(settings.capabilities())
        assert work is not None
        assert work.assignment.key.operation.value == "preview"
        await WorkerRunner(client, executor, cache, settings.capabilities()).execute(work)
        cached = await cache.get(work.assignment.key, now=datetime.now(UTC))
        assert cached is not None and cached.preview is not None
        await client.submit(work, cached)
        async with coordinator.transaction.sessions() as session:
            rows = (
                await session.scalars(
                    select(OutboxRow).where(
                        OutboxRow.source_id == media_id,
                    )
                )
            ).all()
            accepted = [row for row in rows if row.payload["state"] == "available"]
            assert len(accepted) == 1
            assert accepted[0].payload["kind"] == "preview"
            assert accepted[0].payload["source_revision"] == 1
        deleted = await processing_http.post(
            "/internal/media/deleted",
            headers=internal,
            json={
                "event_id": str(uuid4()),
                "media_id": str(media_id),
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        deleted.raise_for_status()
        assert await client.revocations(settings.capabilities(), (media_id,)) == (media_id,)
        assert await cache.source_ids() == (media_id,)
        await cache.revoke(media_id)
        assert await cache.get(work.assignment.key, now=datetime.now(UTC)) is None
        assert isinstance(work.assignment.attempt_id, UUID)
    finally:
        cache.close()
