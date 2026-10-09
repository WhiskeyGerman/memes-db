"""REST query-inference требует паспорт фактически выполненной модели."""

import json
from dataclasses import asdict
from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest

from memes_platform.infrastructure.http import InternalHttpClient
from memes_search.application.dto.projections import EmbeddingVersion
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.infrastructure.clients.inference import ProcessingRestGateway


async def test_query_inference_carries_actual_model_passport(
    vector_version: EmbeddingVersion,
) -> None:
    task_id = uuid4()

    def respond(request: httpx.Request) -> httpx.Response:
        submitted = json.loads(request.content)
        assert submitted["feature_version"] == asdict(vector_version)
        return httpx.Response(
            HTTPStatus.OK,
            json={
                "task_id": str(task_id),
                "status": "succeeded",
                "embedding": {
                    "values": [1.0, 0.0],
                    "space_id": "fixture-space",
                    "feature_version": asdict(vector_version),
                },
            },
        )

    client = InternalHttpClient(
        base_url="http://processing.invalid",
        internal_api_key=uuid4().hex,
        transport=httpx.MockTransport(respond),
    )
    gateway = ProcessingRestGateway(
        client,
        wait_seconds=0,
        poll_seconds=0.1,
        dimensions=2,
        space_id="fixture-space",
        version=vector_version,
    )
    try:
        result = await gateway.query(task_id, "private query", "fixture-space", uuid4())
        assert result.embedding is not None and result.embedding.version == vector_version
    finally:
        await client.close()


async def test_query_inference_without_passport_is_explicitly_unavailable(
    vector_version: EmbeddingVersion,
) -> None:
    task_id = uuid4()

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.OK,
            json={
                "task_id": str(task_id),
                "status": "succeeded",
                "embedding": {"values": [1.0, 0.0], "space_id": "fixture-space"},
            },
        )

    client = InternalHttpClient(
        base_url="http://processing.invalid",
        internal_api_key=uuid4().hex,
        transport=httpx.MockTransport(respond),
    )
    gateway = ProcessingRestGateway(
        client,
        wait_seconds=0,
        poll_seconds=0.1,
        dimensions=2,
        space_id="fixture-space",
        version=vector_version,
    )
    try:
        with pytest.raises(SearchError) as error:
            await gateway.query(task_id, "private query", "fixture-space", uuid4())
        assert error.value.code is SearchErrorCode.UNAVAILABLE
    finally:
        await client.close()
