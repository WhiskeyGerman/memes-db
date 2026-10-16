import logging
from collections.abc import AsyncIterator
from http import HTTPStatus
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from memes_platform.infrastructure.correlation import request_id
from memes_platform.infrastructure.logging import JsonFormatter, ServiceName
from memes_platform.presentation.body_limit import RequestBodyLimitMiddleware, RequestLimit
from memes_platform.presentation.observability import install_observability


@pytest.fixture
def observed_application() -> FastAPI:
    application = FastAPI()
    install_observability(application, service="fixture")
    application.add_middleware(RequestBodyLimitMiddleware)
    application.add_api_route("/consume", consume_payload, methods=["POST"])
    return application


async def consume_payload(body: dict[str, str]) -> dict[str, str]:
    return body


async def test_request_correlation_and_unmatched_route_have_no_private_labels(
    observed_application: FastAPI,
) -> None:
    identifier = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observed_application), base_url="http://fixture"
    ) as client:
        response = await client.get(
            "/private-user-name?password=secret", headers={"X-Request-ID": identifier}
        )
        assert response.headers["X-Request-ID"] == identifier
        metrics = await client.get("/metrics")
    assert 'route="unmatched"' in metrics.text
    assert "private-user-name" not in metrics.text
    assert "password" not in metrics.text
    assert UUID(metrics.headers["X-Request-ID"])
    assert request_id.get() is None


async def test_chunked_body_is_rejected_without_content_length(
    observed_application: FastAPI,
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"value":"'
        yield b"x" * (RequestLimit.PUBLIC_BYTES + 1)
        yield b'"}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observed_application), base_url="http://fixture"
    ) as client:
        response = await client.post(
            "/consume", content=chunks(), headers={"Content-Type": "application/json"}
        )
    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_log_formatter_includes_correlation_without_url_or_payload() -> None:
    identifier = str(uuid4())
    context = request_id.set(identifier)
    try:
        record = logging.LogRecord("fixture", logging.INFO, "", 0, "worker_done", (), None)
        record.__dict__["task_id"] = str(uuid4())
        secret = uuid4().hex
        record.__dict__["token"] = secret
        formatted = JsonFormatter(
            "fixture-release", service_name=ServiceName.IDENTITY, environment="test"
        ).format(record)
    finally:
        request_id.reset(context)
    assert identifier in formatted
    assert "task_id" in formatted
    assert secret not in formatted


async def test_unknown_http_methods_share_one_bounded_metric_label(
    observed_application: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observed_application), base_url="http://fixture"
    ) as client:
        for method in ("X00001", "X00002"):
            response = await client.request(method, "/consume")
            assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
        metrics = await client.get("/metrics")
    lines = [
        line
        for line in metrics.text.splitlines()
        if line.startswith("memes_http_requests_total{") and 'method="OTHER"' in line
    ]
    assert len(lines) == 1
    assert lines[0].endswith(" 2.0")
    assert "X00001" not in metrics.text and "X00002" not in metrics.text
