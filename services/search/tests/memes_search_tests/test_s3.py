"""Реальная S3-доставка приватных файлов без in-memory подмены SDK."""

import os
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from memes_search.application.dto.projections import EmbeddingVersion
from memes_search.application.dto.queries import SearchLimit
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.infrastructure.configuration import Settings
from memes_search.infrastructure.storage import S3PrivateStorage

pytestmark = pytest.mark.integration


@pytest.fixture
def s3_settings(vector_version: EmbeddingVersion) -> Settings:
    endpoint = os.environ.get("MEMES_SEARCH_TEST_S3_ENDPOINT_URL")
    if endpoint is None:
        pytest.skip("MEMES_SEARCH_TEST_S3_ENDPOINT_URL requires actual S3-compatible storage")
    vector_version = replace(vector_version, dimensions=SearchLimit.EMBEDDING_INDEX_DIMENSIONS)
    return Settings(
        database_url="postgresql+asyncpg://unused",
        internal_api_key=uuid4().hex,
        release_id="search-s3-test",
        accepted_embedding_version=vector_version,
        embedding_dimensions=vector_version.dimensions,
        embedding_space_id=vector_version.space,
        catalog_url="http://catalog.invalid",
        identity_url="http://identity.invalid",
        processing_url="http://processing.invalid",
        s3_endpoint_url=endpoint,
        s3_bucket=os.environ["MEMES_SEARCH_TEST_S3_BUCKET"],
        s3_access_key=os.environ["MEMES_SEARCH_TEST_S3_ACCESS_KEY"],
        s3_secret_key=os.environ["MEMES_SEARCH_TEST_S3_SECRET_KEY"],
    )


@pytest.fixture
def storage(s3_settings: Settings) -> S3PrivateStorage:
    return S3PrivateStorage(s3_settings)


async def body() -> AsyncIterator[bytes]:
    yield b"private "
    yield b"sample bytes"


async def test_actual_s3_upload_signed_download_and_delete(storage: S3PrivateStorage) -> None:
    key = f"samples/{uuid4()}/{uuid4()}"
    try:
        file = await storage.put(key, uuid4(), body(), 1024)
        assert file.size_bytes == len(b"private sample bytes")
        url = await storage.download_url(key, 30)
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            assert response.content == b"private sample bytes"
        await storage.delete(key)
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            assert response.is_client_error
    finally:
        await storage.delete(key)
        storage.close()


async def test_actual_s3_rejects_overflow_before_object_upload(storage: S3PrivateStorage) -> None:
    try:
        with pytest.raises(SearchError) as error:
            await storage.put(f"samples/{uuid4()}/{uuid4()}", uuid4(), body(), 1)
        assert error.value.code is SearchErrorCode.SAMPLE_TOO_LARGE
    finally:
        storage.close()
