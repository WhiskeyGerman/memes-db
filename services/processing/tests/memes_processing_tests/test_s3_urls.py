from urllib.parse import parse_qs, urlparse

import pytest

from memes_processing.infrastructure.storage.s3 import S3Artifacts
from memes_processing_tests.fixtures import CoordinatorFixture


@pytest.mark.asyncio
async def test_worker_urls_use_external_host_and_bounded_get_expiry(
    coordinator: CoordinatorFixture,
) -> None:
    settings = coordinator.settings.model_copy(
        update={
            "s3_endpoint_url": "http://s3:8333",
            "s3_public_endpoint_url": "https://media.example",
        }
    )
    storage = S3Artifacts(settings)
    try:
        download = urlparse(await storage.download_url("originals/source/file"))
        upload = urlparse(await storage.upload_url("attempts/source/task/attempt/result.json"))
        assert download.hostname == upload.hostname == "media.example"
        assert download.scheme == upload.scheme == "https"
        assert parse_qs(download.query)["X-Amz-Expires"] == ["60"]
        assert parse_qs(upload.query)["X-Amz-Expires"] == ["720"]
        assert storage.client.meta.endpoint_url == "http://s3:8333"
    finally:
        await storage.close()
