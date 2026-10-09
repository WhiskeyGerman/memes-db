import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from http import HTTPStatus
from io import BytesIO
from os import environ
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from memes_kernel.values import FileRef, Number, Sha256
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    CoordinatorPolicy,
    MediaKind,
    OutgoingEvent,
    Payload,
    Source,
    SourceType,
)
from memes_processing.application.ports.external import IArtifactStorage, IEventDelivery
from memes_processing.application.ports.repository import IProcessingAction, ITransaction
from memes_processing.application.use_cases.delivery import CleanArtifact
from memes_processing.domain.features import FeatureKind
from memes_processing.infrastructure.config.settings import ProcessingSettings
from memes_processing.infrastructure.http.identity import RESTIdentityGateway
from memes_processing.infrastructure.http.security import DeviceCredentials
from memes_processing.infrastructure.persistence.models import Base
from memes_processing.infrastructure.persistence.repository import PostgresTransaction
from memes_processing.infrastructure.storage.preview import PillowPreviewVerifier
from memes_processing.presentation.main import Runtime, create_app


class MemoryArtifacts(IArtifactStorage):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def download_url(self, key: str) -> str:
        return f"https://objects.invalid/{key}"

    async def upload_url(self, key: str) -> str:
        return f"https://objects.invalid/{key}"

    async def result(self, key: str, *, maximum_bytes: int) -> bytes:
        value = self.objects[key]
        if len(value) > maximum_bytes:
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
        return value


@dataclass
class CoordinatorFixture:
    engine: AsyncEngine
    transaction: PostgresTransaction
    policy: CoordinatorPolicy
    credentials: DeviceCredentials
    storage: MemoryArtifacts
    settings: ProcessingSettings


@pytest_asyncio.fixture
async def coordinator() -> AsyncIterator[CoordinatorFixture]:
    url = environ.get("MEMES_PROCESSING_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MEMES_PROCESSING_TEST_DATABASE_URL required for real PostgreSQL tests")
    if not url.rsplit("/", 1)[-1].endswith("_test"):
        raise ValueError("integration database must end with _test")
    credential, _digest = DeviceCredentials().issue()
    settings = ProcessingSettings(
        database_url=url,
        internal_api_key="test-internal-key-" * 3,
        release_id="test",
        identity_url="https://identity.invalid",
        catalog_url="https://catalog.invalid",
        search_url="https://search.invalid",
        s3_bucket="test",
        s3_access_key=credential,
        s3_secret_key=credential,
    )
    engine = create_engine(url, pool_size=5, max_overflow=5)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield CoordinatorFixture(
        engine=engine,
        transaction=PostgresTransaction(session_factory(engine)),
        policy=settings.policy(),
        credentials=DeviceCredentials(),
        storage=MemoryArtifacts(),
        settings=settings,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def jpeg_preview() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, "JPEG")
    return output.getvalue()


@pytest_asyncio.fixture
async def processing_http(coordinator: CoordinatorFixture) -> AsyncIterator[httpx.AsyncClient]:
    settings = coordinator.settings
    app = create_app(settings)
    asgi = httpx.ASGITransport(app=app)

    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "objects.invalid":
            assert "Authorization" not in request.headers
            assert "X-Internal-Key" not in request.headers
            key = request.url.path.removeprefix("/")
            if request.method == "PUT":
                assert request.headers["Content-Type"] in {"application/json", "image/jpeg"}
                coordinator.storage.objects[key] = await request.aread()
                return httpx.Response(HTTPStatus.OK)
            return httpx.Response(HTTPStatus.OK, content=coordinator.storage.objects[key])
        return await asgi.handle_async_request(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                HTTPStatus.OK,
                json={"user_id": str(uuid4()), "login": "operator", "roles": ["user", "operator"]},
            )
        )
    ) as identity:
        app.state.runtime = Runtime(
            settings=settings,
            transaction=coordinator.transaction,
            storage=coordinator.storage,
            client=identity,
            policy=coordinator.policy,
            verifier=PillowPreviewVerifier(),
            identity=RESTIdentityGateway(identity, settings),
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(route),
            base_url="https://processing.invalid",
        ) as client:
            yield client


class MemoryDelivery(IEventDelivery):
    async def deliver(self, event: OutgoingEvent) -> None:
        return None


@dataclass
class QueuedTransaction(ITransaction):
    remaining: int = 0
    failed: bool = False
    cleanup_calls: int = 0
    delivered: int = 0
    stop_on_cleanup: asyncio.Event | None = None

    async def run(self, action: IProcessingAction) -> Payload:
        if isinstance(action, CleanArtifact):
            self.cleanup_calls += 1
            if self.stop_on_cleanup is not None:
                self.stop_on_cleanup.set()
            return {"processed": 0}
        if not self.remaining:
            return {"processed": 0, "retried_event_ids": []}
        self.remaining -= 1
        if self.failed:
            return {"processed": 1, "retried_event_ids": [str(uuid4())]}
        self.delivered += 1
        return {"processed": 1, "retried_event_ids": []}


@pytest.fixture
def queued_transaction() -> QueuedTransaction:
    return QueuedTransaction()


@pytest.fixture
def memory_delivery() -> MemoryDelivery:
    return MemoryDelivery()


@pytest.fixture
def memory_artifacts() -> MemoryArtifacts:
    return MemoryArtifacts()


@pytest.fixture
def source(now: datetime) -> Source:
    return Source(
        id=uuid4(),
        type=SourceType.MEDIA,
        kind=MediaKind.IMAGE,
        file=FileRef(id=uuid4(), sha256=Sha256(value="a" * 64), size_bytes=128),
        object_key="test/source.png",
        created_at=now,
    )


@pytest.fixture
def private_sample(source: Source, now: datetime) -> Source:
    return replace(source, type=SourceType.SAMPLE, expires_at=now + timedelta(minutes=10))


@pytest.fixture
def sample_feature_results(coordinator: CoordinatorFixture) -> dict[FeatureKind, Payload]:
    results: dict[FeatureKind, Payload] = {
        FeatureKind.OCR: {"fragments": [{"text": "meme", "language": "en", "span": None}]}
    }
    for kind in (FeatureKind.VISUAL_EMBEDDING, FeatureKind.TEXT_EMBEDDING):
        dimensions = coordinator.policy.versions[kind].dimensions
        assert dimensions is not None
        results[kind] = {
            "items": [
                {
                    "values": [
                        float(Number.ONE) if index == Number.ZERO else float(Number.ZERO)
                        for index in range(dimensions)
                    ],
                    "span": None,
                }
            ]
        }
    return results
