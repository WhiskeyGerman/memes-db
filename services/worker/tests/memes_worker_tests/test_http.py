from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from memes_worker.application.dto.runtime import (
    WorkAssignment,
    WorkerFailure,
    WorkerTransportError,
    WorkResult,
)
from memes_worker.infrastructure.config.settings import WorkerSettings
from memes_worker.infrastructure.http.client import RESTCoordinatorClient


@pytest.fixture
def worker_settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        coordinator_url="https://coordinator.invalid",
        worker_id=uuid4(),
        token="test-individual-device-secret-" * 3,
        release_id="test",
        cache_directory=tmp_path,
    )


@pytest.mark.asyncio
async def test_credentials_only_sent_to_coordinator(
    work: WorkAssignment, result: WorkResult, worker_settings: WorkerSettings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "coordinator.invalid":
            assert request.headers["Authorization"] == f"Bearer {worker_settings.token}"
            return httpx.Response(HTTPStatus.OK, json={"accepted": True})
        assert "Authorization" not in request.headers
        return httpx.Response(HTTPStatus.OK)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await RESTCoordinatorClient(client, worker_settings).submit(work, result)


@pytest.mark.asyncio
async def test_conflict_revokes_local_ownership(
    work: WorkAssignment, worker_settings: WorkerSettings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.CONFLICT, json={"detail": "expired"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WorkerTransportError) as error:
            await RESTCoordinatorClient(client, worker_settings).renew(work)
    assert error.value.reason is WorkerFailure.OWNERSHIP_LOST


def test_insecure_http_requires_explicit_development_option(
    worker_settings: WorkerSettings,
) -> None:
    configured = worker_settings.model_copy(update={"coordinator_url": "http://localhost"})
    with pytest.raises(ValueError):
        configured.capabilities()
    assert (
        configured.model_copy(update={"allow_insecure_http": True}).capabilities()["versions"]
        is not None
    )
