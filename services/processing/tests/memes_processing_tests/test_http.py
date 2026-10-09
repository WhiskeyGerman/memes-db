from http import HTTPStatus

import httpx
import pytest

from memes_processing.infrastructure.config.settings import ProcessingSettings
from memes_processing.infrastructure.http.identity import RESTIdentityGateway
from memes_processing.infrastructure.http.security import DeviceCredentials
from memes_processing.infrastructure.storage.preview import PillowPreviewVerifier
from memes_processing.presentation.main import Runtime, create_app
from memes_processing_tests.fixtures import CoordinatorFixture


@pytest.mark.asyncio
async def test_openapi_and_auth_boundary(coordinator: CoordinatorFixture) -> None:
    credential, _digest = DeviceCredentials().issue()
    settings = ProcessingSettings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        internal_api_key="test-internal-api-key-" * 3,
        release_id="test",
        identity_url="https://identity.invalid",
        catalog_url="https://catalog.invalid",
        search_url="https://search.invalid",
        s3_bucket="test",
        s3_access_key="test-key",
        s3_secret_key=credential,
    )
    app = create_app(settings)
    assert "/v1/workers/{worker_id}/tasks/{task_id}/complete" in app.openapi()["paths"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(HTTPStatus.OK))
    ) as outgoing:
        app.state.runtime = Runtime(
            settings=settings,
            transaction=coordinator.transaction,
            storage=coordinator.storage,
            client=outgoing,
            policy=coordinator.policy,
            verifier=PillowPreviewVerifier(),
            identity=RESTIdentityGateway(outgoing, settings),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/live")).json() == {"status": "alive"}
            assert (await client.get("/internal/status")).status_code == HTTPStatus.FORBIDDEN
            response = await client.get(
                "/internal/status", headers={"X-Internal-Key": settings.internal_api_key}
            )
            assert response.status_code == HTTPStatus.OK
            assert response.json()["pending_publication_tasks"] == 0
            assert (
                await client.post("/v1/workers", json={"name": "device"})
            ).status_code == HTTPStatus.UNAUTHORIZED
