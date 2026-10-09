from collections.abc import AsyncIterator
from http import HTTPMethod

import httpx
import pytest

from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.infrastructure.http import InternalHttpClient


@pytest.fixture(params=["protocol", "timeout", "invalid_json"])
async def failing_client(request: pytest.FixtureRequest) -> AsyncIterator[InternalHttpClient]:
    failure = request.param

    async def handler(received: httpx.Request) -> httpx.Response:
        if failure == "protocol":
            raise httpx.RemoteProtocolError("upstream response incomplete")
        if failure == "timeout":
            raise httpx.ReadTimeout("upstream timed out")
        return httpx.Response(200, content=b"{bad-json")

    client = InternalHttpClient(
        base_url="http://fixture", internal_api_key="k" * 32, transport=httpx.MockTransport(handler)
    )
    try:
        yield client
    finally:
        await client.close()


async def test_known_upstream_failures_are_application_unavailable(
    failing_client: InternalHttpClient,
) -> None:
    with pytest.raises(ApplicationError) as failure:
        await failing_client.request(HTTPMethod.GET, "/fixture")
    assert failure.value.code is ErrorCode.UNAVAILABLE
